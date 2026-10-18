"""Release domain: declarations, gates, version preparation, changelog,
publishing and the release state machine (Issue #1645).

Second domain split out of `test_bootstrap_runner.py` (after
`test_runner_config.py`, Issue #789), moved verbatim with no behavior
change: the tests exercise `orbi.release`, `orbi.release_git` and
`orbi.release_notes` through their public entry points. The shared
scaffolding lives in `tests/fakes/bootstrap_release.py`; helpers that
several domains use (`make_local_remote_pair`, `_git_output`) keep their
single home in `test_bootstrap_runner.py` and are imported here.
"""
import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.github as github
import orbi.journal as journal
import orbi.milestone as milestone
import orbi.release as release
import orbi.release_git as release_git
import orbi.runner as runner
from orbi import config as config_domain
from orbi import failure
from orbi import language
from seam import seam
from tests.fakes.bootstrap_release import (
    _EMPTY_SCOPE_BODY,
    _NEITHER_SCOPE_BODY,
    LEGACY_TEST_COMMAND_DECLARATION_BODY,
    MILESTONE_ISSUES_COMMAND,
    MILESTONE_LIST_COMMAND,
    RELEASE_DECLARATION_BODY,
    RELEASE_MILESTONE_DECLARATION_BODY,
    _milestone,
    commit_file,
    git_out,
    without_include,
    make_gate_gh,
    make_milestone_gh,
    make_registration_lag_gh,
    make_release_gh,
    make_release_process_env,
    make_release_race_clones,
    make_release_range_repo,
    make_scope_gh,
    mock_release_range_gh,
)
from tests.fakes.bootstrap_delivery import _git_output, make_local_remote_pair
from tests.test_progress_wiring import make_fake_gh


def test_parse_release_declaration_returns_all_fields():
    decl = release.parse_release_declaration(RELEASE_DECLARATION_BODY)
    assert decl == {
        "version": "v0.3.0",
        "base_branch": "main",
        "scope": [123, 124],
        "scope_from_milestone": None,
        "version_file": "pyproject.toml",
    }


def test_parse_release_declaration_rejects_a_legacy_test_command():
    """Issue #805: the legacy tolerated field is gone — the declaration
    carries NO test contract (release acceptance is the GitHub Actions
    CI result on the release commit), and an unknown field fails fast
    instead of being silently ignored."""
    with pytest.raises(ValueError, match="unknown field"):
        release.parse_release_declaration(
            LEGACY_TEST_COMMAND_DECLARATION_BODY,
        )


def test_parse_release_declaration_rejects_test_timeout_seconds():
    """Issue #569: the local test-execution path is gone; the field that
    parameterized its timeout is no longer part of the contract."""
    body = RELEASE_DECLARATION_BODY.replace(
        "- scope:", "- test_timeout_seconds: 42\n- scope:",
    )
    with pytest.raises(ValueError, match="unknown field"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_accepts_package_json_version_file():
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n", "- version: v0.3.0\n- version_file: package.json\n",
    )
    assert release.parse_release_declaration(body)["version_file"] == "package.json"


def test_parse_release_declaration_accepts_none_version_file():
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n", "- version: v0.3.0\n- version_file: none\n",
    )
    assert release.parse_release_declaration(body)["version_file"] == "none"


@pytest.mark.parametrize(
    "version_file",
    ["pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties",
     "Cargo.toml", "composer.json", "pubspec.yaml"],
)
def test_parse_release_declaration_accepts_ecosystem_version_file(version_file):
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n",
        f"- version: v0.3.0\n- version_file: {version_file}\n",
    )
    assert release.parse_release_declaration(body)["version_file"] == version_file


def test_parse_release_declaration_rejects_invalid_version_file():
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n", "- version: v0.3.0\n- version_file: version.txt\n",
    )
    with pytest.raises(ValueError, match="version_file"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_allows_milestone_only_tickets():
    assert release.parse_release_declaration("no section here\n") == {}


def test_resolve_release_declaration_derives_milestone_contract(monkeypatch):
    monkeypatch.setattr(
        release, "run_command", lambda *args, **kwargs: "pyproject.toml\n",
    )
    config = Mock(base_branch="main", repositories=[])
    declaration = release.resolve_release_declaration(
        {"milestone": {"title": "v0.5.8"}}, {}, config, "o/r",
        Path("/repo"), "abc123",
    )
    assert declaration == {
        "version": "v0.5.8", "base_branch": "main", "scope": [],
        "scope_from_milestone": "v0.5.8", "version_file": "pyproject.toml",
    }


def test_resolve_release_declaration_detects_node_version_file(monkeypatch):
    monkeypatch.setattr(
        release, "run_command", lambda *args, **kwargs: "package.json\n",
    )
    config = Mock(base_branch="main", repositories=[])
    result = release.resolve_release_declaration(
        {"milestone": {"title": "v0.5.8"}}, {}, config, "o/r",
        Path("/repo"), "abc123",
    )
    assert result["version_file"] == "package.json"


def test_resolve_release_declaration_requires_explicit_version_file(monkeypatch):
    monkeypatch.setattr(release, "run_command", lambda *args, **kwargs: "README.md\n")
    config = Mock(base_branch="main", repositories=[])
    with pytest.raises(ValueError, match="version_file"):
        release.resolve_release_declaration(
            {"milestone": {"title": "v0.5.8"}}, {}, config, "o/r",
            Path("/repo"), "abc123",
        )


def test_resolve_release_declaration_uses_the_fused_base_branch(
        monkeypatch):
    """The config a release run receives is already fused:
    repository entry -> .github/orbi.toml policy override. The inline
    entry lookup re-read the RAW entry, discarding the policy layer:
    policy=develop + entry=main resolved dev to develop and release to
    main — freezing and pushing the release onto the wrong branch."""
    monkeypatch.setattr(
        release, "run_command", lambda *args, **kwargs: "pyproject.toml\n",
    )
    config = Mock(
        base_branch="develop",
        repositories=[{"github": "o/r", "base_branch": "main"}],
    )
    declaration = release.resolve_release_declaration(
        {"milestone": {"title": "v0.5.8"}}, {}, config, "o/r",
        Path("/repo"), "abc123",
    )
    assert declaration["base_branch"] == "develop"


def test_resolve_release_declaration_rejects_non_version_milestone_titles(
    monkeypatch,
):
    """The version format must fail HERE, before the delivery/CI wait
    gates burn their budgets: the only format check used to live in
    prepare_release_version, which runs after two <=1800s waits."""
    monkeypatch.setattr(
        release, "run_command", lambda *args, **kwargs: "pyproject.toml\n",
    )
    config = Mock(base_branch="main", repositories=[])
    with pytest.raises(ValueError, match="v-prefixed"):
        release.resolve_release_declaration(
            {"milestone": {"title": "Release 5 beta"}}, {}, config, "o/r",
            Path("/repo"), "abc123",
        )


def test_resolve_release_declaration_rejects_version_mismatch():
    config = Mock(base_branch="main", repositories=[])
    with pytest.raises(ValueError, match="does not match.*Milestone"):
        release.resolve_release_declaration(
            {"milestone": {"title": "v0.5.8"}},
            {"version": "v0.5.7", "base_branch": "main",
             "scope_from_milestone": "v0.5.7", "scope": [],
             "version_file": "none"},
            config, "o/r", Path("/repo"), "abc123",
        )


def test_resolve_release_declaration_requires_milestone_or_scope():
    config = Mock(base_branch="main", repositories=[])
    with pytest.raises(ValueError, match="Milestone or an explicit `scope`"):
        release.resolve_release_declaration(
            {"milestone": None},
            {"version": "v0.5.8", "base_branch": "main",
             "version_file": "none"},
            config, "o/r", Path("/repo"), "abc123",
        )


def test_parse_release_declaration_requires_version():
    body = RELEASE_DECLARATION_BODY.replace("- version: v0.3.0\n", "")
    with pytest.raises(ValueError, match="version"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_requires_base_branch():
    body = RELEASE_DECLARATION_BODY.replace("- base_branch: main\n", "")
    with pytest.raises(ValueError, match="base_branch"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_requires_scope():
    lines = [line for line in RELEASE_DECLARATION_BODY.splitlines()
             if not line.strip().startswith("- #")]
    body = "\n".join(line for line in lines if line.strip() != "- scope:") + "\n"
    with pytest.raises(ValueError, match="scope"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_empty_scope():
    lines = [line for line in RELEASE_DECLARATION_BODY.splitlines()
             if not line.strip().startswith("- #")]
    with pytest.raises(ValueError, match="scope"):
        release.parse_release_declaration("\n".join(lines) + "\n")


def test_parse_release_declaration_rejects_duplicate_field():
    body = RELEASE_DECLARATION_BODY.replace(
        "\n## Notes", "\n- version: v9.9.9\n\n## Notes",
    )
    with pytest.raises(ValueError, match="version"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_unknown_key():
    body = RELEASE_DECLARATION_BODY.replace(
        "\n## Notes", "\n- channel: stable\n\n## Notes",
    )
    with pytest.raises(ValueError, match="channel"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_version_with_space():
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0", "- version: v0.3 .0",
    )
    with pytest.raises(ValueError, match="version"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_empty_value():
    body = RELEASE_DECLARATION_BODY.replace("- base_branch: main",
                                            "- base_branch:")
    with pytest.raises(ValueError, match="base_branch"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_malformed_scope_item():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - #abc")
    with pytest.raises(ValueError, match="scope item"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_scope_item_without_hash():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - 123")
    with pytest.raises(ValueError, match="scope item"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_zero_scope_item():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - #0")
    with pytest.raises(ValueError, match="scope item"):
        release.parse_release_declaration(body)


# Issue #831: every declaration parse error names the violation AND the
# expected form, so the user whose ticket lands `ai-blocked` can repair
# the Issue body by copy-paste. The example constant is itself valid
# parser input (locked below) and the `version_file` value range quoted
# in the errors is generated from RELEASE_VERSION_FILE_OPTIONS — the
# error copy cannot drift from the parser's supported set.

def test_release_declaration_example_is_valid_parser_input():
    assert release.parse_release_declaration(
        release.RELEASE_DECLARATION_EXAMPLE,
    ) == {
        "version": "v1.2.0",
        "base_branch": "main",
        "scope": [53, 54],
        "scope_from_milestone": None,
        "version_file": "pyproject.toml",
    }


def test_missing_section_is_an_empty_override():
    assert release.parse_release_declaration("release 1.2.0\n") == {}


@pytest.mark.parametrize(
    ("body", "expected_form"),
    [
        (
            RELEASE_DECLARATION_BODY.replace("- version: v0.3.0\n", ""),
            "- version: v1.2.0",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("- base_branch: main",
                                             "- base_branch:"),
            "- base_branch: main",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "\n## Notes", "\n- version: v9.9.9\n\n## Notes",
            ),
            "- version: v1.2.0",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "\n## Notes", "\n- channel: stable\n\n## Notes",
            ),
            "e.g. `- version: v1.2.0`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("## Release\n",
                                             "## Release\n\n- broken\n"),
            "e.g. `- version: v1.2.0`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("- scope:\n", "- scope: 123\n"),
            "- scope:` followed by `  - #53` item lines",
        ),
        (_EMPTY_SCOPE_BODY, "- scope:` followed by `  - #53` item lines"),
        (
            RELEASE_DECLARATION_BODY.replace("  - #123", "  - #0"),
            "expected form: `  - #53`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("  - #123", "  - #abc"),
            "e.g. `  - #53`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("  - #123", "hello"),
            "e.g. `  - #53`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace("## Release\n",
                                             "## Release\n\nhello\n"),
            "e.g. `- version: v1.2.0`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "- base_branch: main",
                "- base_branch: main\n- scope_from_milestone: v0.3.0",
            ),
            "`- scope_from_milestone: v1.2.0`",
        ),
        (_NEITHER_SCOPE_BODY, "`- scope_from_milestone: v1.2.0`"),
        (
            RELEASE_DECLARATION_BODY.replace("- version: v0.3.0",
                                             "- version: v0.3 .0"),
            "- version: v1.2.0",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "- scope:\n  - #123\n  - #124\n",
                "- scope_from_milestone:\n",
            ),
            "`- scope_from_milestone: v1.2.0`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "- scope:\n  - #123\n  - #124\n",
                "- scope_from_milestone: v0.3 .0\n",
            ),
            "`- scope_from_milestone: v1.2.0`",
        ),
        (
            RELEASE_DECLARATION_BODY.replace(
                "- version: v0.3.0\n",
                "- version: v0.3.0\n- version_file: version.txt\n",
            ),
            ", ".join(release.RELEASE_VERSION_FILE_OPTIONS),
        ),
    ],
)
def test_parse_error_messages_carry_the_expected_form(body, expected_form):
    with pytest.raises(ValueError) as excinfo:
        release.parse_release_declaration(body)
    assert expected_form in str(excinfo.value)


def test_parse_release_declaration_scope_from_milestone():
    decl = release.parse_release_declaration(RELEASE_MILESTONE_DECLARATION_BODY)
    assert decl == {
        "version": "v0.3.0",
        "base_branch": "main",
        "scope": [],
        "scope_from_milestone": "v0.3.0",
        "version_file": "pyproject.toml",
    }


# Issue #740: the declared (or defaulted) `version_file` is proven to
# exist in the frozen release tree at claim time — a mismatch used to
# surface only at the version-write step, AFTER the gates and the scope
# verification, burning the ticket ai-blocked with a bare
# FileNotFoundError.

def test_verify_release_version_file_passes_when_the_file_exists(monkeypatch):
    commands = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: commands.append((command, kwargs))
        or "pyproject.toml\npackage.json\n",
    )
    release.verify_release_version_file(Path("/r"), "abc123", "pyproject.toml")
    assert commands == [(["git", "ls-tree", "--name-only", "abc123"],
                         {"cwd": Path("/r")})]


def test_verify_release_version_file_skips_none_without_probing(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(
            AssertionError(f"unexpected command: {command}")),
    )
    release.verify_release_version_file(Path("/r"), "abc123", "none")


def test_verify_release_version_file_lists_existing_supported_files(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: "package.json\nCargo.toml\nREADME.md\n",
    )
    with pytest.raises(RuntimeError) as excinfo:
        release.verify_release_version_file(
            Path("/r"), "abc123", "pyproject.toml",
        )
    message = str(excinfo.value)
    assert "pyproject.toml" in message
    assert "abc123" in message
    assert "package.json" in message
    assert "Cargo.toml" in message
    assert "version_file: none" in message


def test_verify_release_version_file_without_supported_file_points_at_none(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "README.md\n",
    )
    with pytest.raises(RuntimeError) as excinfo:
        release.verify_release_version_file(
            Path("/r"), "abc123", "package.json",
        )
    assert "package.json" in str(excinfo.value)
    assert "version_file: none" in str(excinfo.value)


def test_parse_release_declaration_manual_scope_has_no_milestone():
    decl = release.parse_release_declaration(RELEASE_DECLARATION_BODY)
    assert decl["scope_from_milestone"] is None


def test_parse_release_declaration_rejects_scope_and_scope_from_milestone():
    body = RELEASE_MILESTONE_DECLARATION_BODY.replace(
        "\n## Notes", "\n- scope:\n  - #123\n\n## Notes",
    )
    with pytest.raises(ValueError, match="exactly one"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_empty_scope_from_milestone():
    body = RELEASE_MILESTONE_DECLARATION_BODY.replace(
        "- scope_from_milestone: v0.3.0", "- scope_from_milestone:",
    )
    with pytest.raises(ValueError, match="scope_from_milestone"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_scope_from_milestone_with_space():
    body = RELEASE_MILESTONE_DECLARATION_BODY.replace(
        "- scope_from_milestone: v0.3.0", "- scope_from_milestone: v0.3 .0",
    )
    with pytest.raises(ValueError, match="scope_from_milestone"):
        release.parse_release_declaration(body)


def test_is_release_detects_the_label():
    issue = {"number": 99, "labels": [
        {"name": "ai-ready"}, {"name": "ai-release"},
    ]}
    assert claim.is_release(issue) is True


def test_is_release_false_without_label_or_malformed():
    assert claim.is_release({"number": 1, "labels": [{"name": "ai-ready"}]}) is False
    assert claim.is_release({"number": 1}) is False
    assert claim.is_release({"number": 1, "labels": "ai-release"}) is False


def test_verify_release_scope_evidence_for_pr_and_issue(monkeypatch):
    make_scope_gh(
        monkeypatch,
        pr_state_map={123: ("MERGED", "aaa111")},
        issue_state_map={124: "CLOSED"},
    )
    evidence = release.verify_release_scope("o/r", [123, 124], Path("/repo"), "release123")
    assert evidence == [
        "PR #123 merged (mergeCommit=aaa111)",
        "Issue #124 closed",
    ]


def test_verify_release_scope_annotates_not_planned_and_counts_completed(
        monkeypatch):
    """Issue #707: a CLOSED Issue counts as released only when its
    stateReason is COMPLETED; a NOT_PLANNED closure (duplicate / won't
    fix) is never a delivery — the Scope evidence annotates it as
    excluded instead of silently counting it in."""
    make_scope_gh(
        monkeypatch,
        issue_state_map={124: "CLOSED", 125: "CLOSED"},
        issue_reason_map={124: "COMPLETED", 125: "NOT_PLANNED"},
    )
    evidence = release.verify_release_scope(
        "o/r", [124, 125], Path("/repo"), "release123",
    )
    assert evidence == [
        "Issue #124 closed",
        "Issue #125 closed (not planned, excluded)",
    ]


def test_verify_release_scope_fails_on_unmerged_pr(monkeypatch):
    make_scope_gh(monkeypatch, pr_state_map={123: ("OPEN", None)})
    with pytest.raises(RuntimeError, match="PR #123 is not merged"):
        release.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_rejects_merged_pr_outside_release_base(
        monkeypatch):
    """A PR merged on another branch is not evidence for this tag."""
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", "other123")})
    original = runner.run_command

    def not_an_ancestor(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", not_an_ancestor)
    monkeypatch.setattr(seam, "run_command", not_an_ancestor)
    with pytest.raises(RuntimeError, match="not contained in release commit"):
        release.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_reraises_git_ancestry_check_failure(
        monkeypatch):
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", "aaa111")})
    original = runner.run_command

    def git_failure(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(128, command, stderr="bad object")
        return original(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", git_failure)
    monkeypatch.setattr(seam, "run_command", git_failure)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        release.verify_release_scope("o/r", [123], Path("/repo"), "release123")
    assert excinfo.value.stderr == "bad object"


def test_verify_release_scope_fails_on_unclosed_issue(monkeypatch):
    make_scope_gh(monkeypatch, issue_state_map={124: "OPEN"})
    with pytest.raises(RuntimeError, match="Issue #124 is not closed"):
        release.verify_release_scope("o/r", [124], Path("/repo"), "release123")


def test_verify_release_scope_fails_on_merged_pr_without_merge_commit(
        monkeypatch):
    # A MERGED PR without merge-commit evidence is not evidence.
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", None)})
    with pytest.raises(RuntimeError, match="no merge commit evidence"):
        release.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_fails_on_unknown_item(monkeypatch):
    make_scope_gh(monkeypatch)
    with pytest.raises(RuntimeError, match="neither a PR nor an Issue"):
        release.verify_release_scope("o/r", [999], Path("/repo"), "release123")


def test_verify_release_scope_reraises_real_gh_failure(monkeypatch):
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 403: rate limited",
        )
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    with pytest.raises(subprocess.CalledProcessError):
        release.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_derive_release_scope_flattens_multi_page_results(monkeypatch):
    """发布 scope 的 Milestone 与 issues 查询必须用
    `gh api --paginate --slurp` + `parse_paginated_issue_array` 展平各页：
    Milestone 的 issue 超过一页（per_page=100）后，单页 `json.loads`
    解析必崩，发布 scope 推导对该 Milestone 永久失败。"""
    milestones = [{
        "number": 5, "title": "v0.9.0", "state": "closed",
        "open_issues": 0, "closed_issues": 105,
        "url": "https://api.github.com/repos/o/r/milestones/5",
        "html_url": "https://github.com/o/r/milestone/5",
    }]
    page_one = [{"number": n, "title": f"issue {n}", "state": "closed"}
                for n in range(1, 101)]
    page_two = [{"number": n, "title": f"issue {n}", "state": "closed"}
                for n in range(101, 106)]

    def fake_run(command, **kwargs):
        # gh 的输出契约：--paginate --slurp 把各页收拢进一个外层数组。
        assert "--slurp" in command
        path = command[2]
        if path.startswith("repos/o/r/milestones?"):
            return json.dumps([milestones])
        if "state=open" in path:
            return json.dumps([[]])
        return json.dumps([page_one, page_two])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v0.9.0",
    )
    assert scope == list(range(1, 106))
    assert open_evidence == []


def test_milestone_gh_fake_rejects_unexpected_command(monkeypatch):
    make_milestone_gh(monkeypatch, milestones=[])
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "api", "repos/o/r/issues?state=all"])
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["git", "status"])
    with pytest.raises(AssertionError, match="must not query /pulls"):
        runner.run_command([
            "gh", "api", "repos/o/r/pulls?state=closed&milestone=5",
        ])


def test_derive_release_scope_from_milestone_filters_pr_objects_from_issues(
        monkeypatch):
    calls = make_milestone_gh(
        monkeypatch,
        milestones=[{"number": 5, "title": "v0.3.0", "state": "open"}],
        items_by_milestone={5: {
            "issues_closed": [
                {"number": 160, "title": "Deliver A", "state": "closed"},
                {"number": 168, "title": "Deliver B", "state": "closed"},
                # GitHub's issues endpoint can include PR objects. They
                # must not become scope items; PRs are linked later from
                # each scoped issue's closing references.
                {"number": 170, "title": "External PR", "state": "closed",
                 "pull_request": {"merged_at": "2026-09-01T00:00:00Z"}},
            ],
        }},
    )
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v0.3.0",
    )
    assert scope == [160, 168]
    assert open_evidence == []
    assert not any("/pulls?" in command[2] for command in calls)


def test_derive_release_scope_from_milestone_lists_open_items(monkeypatch):
    make_milestone_gh(
        monkeypatch,
        milestones=[{"number": 5, "title": "v0.3.0", "state": "open"}],
        items_by_milestone={5: {
            "issues_closed": [
                {"number": 160, "title": "Deliver A", "state": "closed"},
            ],
            "issues_open": [
                {"number": 255, "title": "Still open work",
                 "state": "open"},
            ],
        }},
    )
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v0.3.0",
    )
    assert scope == [160]
    assert open_evidence == ["open Issue #255 Still open work"]


def test_derive_release_scope_from_milestone_fails_when_missing(monkeypatch):
    make_milestone_gh(monkeypatch, milestones=[])
    with pytest.raises(
            RuntimeError, match="no Milestone with the exact title"):
        release.derive_release_scope_from_milestone("o/r", "v0.9.9")


def test_derive_release_scope_from_milestone_fails_on_ambiguous_title(
        monkeypatch):
    make_milestone_gh(
        monkeypatch,
        milestones=[
            {"number": 5, "title": "v0.3.0", "state": "open"},
            {"number": 6, "title": "v0.3.0", "state": "open"},
        ],
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        release.derive_release_scope_from_milestone("o/r", "v0.3.0")


def test_derive_release_scope_from_milestone_empty_scope(monkeypatch):
    make_milestone_gh(
        monkeypatch,
        milestones=[{"number": 5, "title": "v0.3.0", "state": "open"}],
        items_by_milestone={5: {
            "issues_open": [
                {"number": 255, "title": "Still open work", "state": "open"},
            ],
        }},
    )
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v0.3.0",
    )
    assert scope == []
    assert open_evidence == ["open Issue #255 Still open work"]


def test_derive_release_scope_from_milestone_excludes_release_issue(
        monkeypatch):
    # Issue #818: the release Issue itself is necessarily still open
    # when the notes are generated (it closes after the release), so
    # listing it as "NOT released" is a self-referential false
    # statement. It must be exempted like the #663 gate does.
    make_milestone_gh(
        monkeypatch,
        milestones=[{"number": 5, "title": "v1.0.0", "state": "open"}],
        items_by_milestone={5: {
            "issues_closed": [
                {"number": 50, "title": "Deliver A", "state": "closed"},
            ],
            "issues_open": [
                {"number": 52, "title": "Release v1.0.0", "state": "open"},
            ],
        }},
    )
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v1.0.0", release_issue=52,
    )
    assert scope == [50]
    assert open_evidence == []


def test_derive_release_scope_from_milestone_keeps_genuinely_open_items(
        monkeypatch):
    # Issue #818: only the release Issue is exempt — a real unfinished
    # Issue in the milestone must still surface as NOT released.
    make_milestone_gh(
        monkeypatch,
        milestones=[{"number": 5, "title": "v1.0.0", "state": "open"}],
        items_by_milestone={5: {
            "issues_closed": [
                {"number": 50, "title": "Deliver A", "state": "closed"},
            ],
            "issues_open": [
                {"number": 52, "title": "Release v1.0.0", "state": "open"},
                {"number": 60, "title": "Unfinished work", "state": "open"},
            ],
        }},
    )
    scope, open_evidence = release.derive_release_scope_from_milestone(
        "o/r", "v1.0.0", release_issue=52,
    )
    assert scope == [50]
    assert open_evidence == ["open Issue #60 Unfinished work"]


def test_check_release_gates_pass_clean(monkeypatch):
    calls = make_gate_gh(
        monkeypatch,
        check_runs=[("tests", "completed", "success"),
                    ("lint", "completed", "skipped")],
    )
    evidence, _ = release.check_release_gates("o/r", "abc123", 99)
    assert evidence == [
        "no open Issue carries ai-in-progress / ai-pr-opened / ai-fix-needed",
        "CI on the release commit: 2 check(s) all success/neutral/skipped",
    ]
    # The gate never queries open PRs (Issue #608): open PRs are queue
    # state, not a release premise.
    assert not any(c[:3] == ["gh", "pr", "list"] for c in calls)
    # The release Issue itself is excluded from the leftover scan:
    # every leftover scan fetched the label's open Issues.
    labels = [c[c.index("--label") + 1] for c in calls
              if c[:3] == ["gh", "issue", "list"]]
    assert labels == ["ai-in-progress", "ai-pr-opened", "ai-fix-needed"]


def test_registration_lag_gh_rejects_unexpected_commands(monkeypatch):
    fake = make_registration_lag_gh(monkeypatch, api_responses=[[]])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "pr", "list"])


def test_check_release_gates_waits_for_late_registered_checks(monkeypatch):
    """repo_has_ci=True 时空 check-runs = 首个 check 尚未注册（Issue
    #657）：gate 2 跑在刚 push 到 base 的版本提交上，GitHub 创建
    CheckRun 有秒级滞后，空列表绝不能立即按 "nothing to gate" 放行——
    必须在同一预算内轮询等首个 check 出现，再按其结论判门。"""
    make_registration_lag_gh(monkeypatch, api_responses=[
        [],                                   # first poll: not registered yet
        [("tests", "completed", "success")],  # second poll: registered, green
    ])
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    evidence, has_ci = release.check_release_gates(
        "o/r", "abc123", 99, repo_has_ci=True,
    )
    assert has_ci is True
    assert not any("nothing to gate" in line for line in evidence)
    assert any("1 check(s) all success" in line for line in evidence)
    assert any("waited" in line for line in evidence)


def test_check_release_gates_times_out_when_ci_never_registers(monkeypatch):
    """repo_has_ci=True 且 check-runs 恒为空：等待宽限耗尽后必须按
    wait-timeout 失败（不是 CI 失败、更不是放行），下 tick 可恢复重试
    （Issue #657）。"""
    make_registration_lag_gh(
        monkeypatch, api_responses=[[], [], [], [], [], []],
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    # Its own type (Issue #1760): the blocked report reads the type, never
    # the message, so a spent wait budget is a human decision.
    with pytest.raises(
        failure.ReleaseGateWaitTimeout, match="wait timeout, not a CI failure",
    ):
        release.check_release_gates(
            "o/r", "abc123", 99,
            repo_has_ci=True, ci_wait_seconds=60,
        )


def test_check_release_gates_empty_without_ci_still_passes(monkeypatch):
    """repo_has_ci=False（门控提交上无任何 check）＝无 CI 仓库：空列表
    仍立即放行，"nothing to gate" 语义保持（Issue #657 的判据只收紧
    有 CI 的仓库）。"""
    make_registration_lag_gh(monkeypatch, api_responses=[[]])
    evidence, has_ci = release.check_release_gates("o/r", "abc123", 99)
    assert has_ci is False
    assert any("nothing to gate" in line for line in evidence)


def test_check_release_gates_excludes_the_release_issue_itself(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-in-progress": [99]},  # the release Issue
        check_runs=[],
    )
    evidence, _ = release.check_release_gates("o/r", "abc123", 99)
    assert evidence[0].startswith("no open Issue carries")


def test_check_release_gates_fails_on_leftover_in_progress(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-in-progress": [7]},
        check_runs=[],
    )
    with pytest.raises(runner.ReleaseDeliveriesWaiting) as excinfo:
        release.check_release_gates("o/r", "abc123", 99)
    assert excinfo.value.issue_numbers == [7]


def test_check_release_gates_fails_on_leftover_fix_needed(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [8]},
        check_runs=[],
    )
    with pytest.raises(runner.ReleaseDeliveriesWaiting) as excinfo:
        release.check_release_gates("o/r", "abc123", 99)
    assert excinfo.value.issue_numbers == [8]


def test_check_release_gates_delivery_wait_timeout_is_not_ci_failure(monkeypatch):
    make_gate_gh(monkeypatch, leftover_labels={"ai-in-progress": [7]}, check_runs=[])
    with pytest.raises(
        failure.ReleaseGateWaitTimeout,
        match="delivery wait timeout, not a CI failure",
    ):
        release.check_release_gates("o/r", "abc123", 99,
                                   delivery_wait_seconds=5,
                                   delivery_waited_seconds=5)


def test_check_release_gates_fails_on_failing_ci(monkeypatch):
    make_gate_gh(
        monkeypatch,
        check_runs=[("tests", "completed", "failure")],
    )
    with pytest.raises(RuntimeError, match="check 'tests' is completed/failure"):
        release.check_release_gates("o/r", "abc123", 99)


def test_check_release_gates_waits_for_pending_ci_then_passes(
        monkeypatch, caplog):
    """Issue #268 regression (the 2026-09-04 v0.3.0 scene): the release
    commit's CI is still running when the gate checks it — a pending
    check is an intermediate state, not a failure. The gate waits,
    emits `release_waiting_ci` journal lines, and passes on the FINAL
    conclusion."""
    pages = [
        [("tests", "in_progress", None)],
        [("tests", "completed", "success")],
    ]

    def fake_run_command(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            page = pages.pop(0)
            if not pages:
                pages.append(page)
            return json.dumps([
                {"name": n, "status": s, "conclusion": c}
                for n, s, c in page
            ])
        return "[]"  # the label scans and the open-PR scan

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    waits = []
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        evidence, _ = release.check_release_gates(
            "o/r", "abc123", 99, on_wait=waits.append,
        )
    assert evidence == [
        "no open Issue carries ai-in-progress / ai-pr-opened / ai-fix-needed",
        "CI on the release commit: 1 check(s) all success/neutral/skipped "
        "(waited 30s for pending checks)",
    ]
    # The wait is observable: one `release_waiting_ci` journal line per
    # poll and the detail passed to the progress-comment callback.
    assert "release_waiting_ci" in caplog.text
    assert "issue=99" in caplog.text
    assert waits == ["check 'tests' is in_progress/None; waited 0s / 1800s"]


def test_check_release_gates_waits_then_fails_on_final_ci_failure(
        monkeypatch):
    """Issue #268: waiting must not soften the gate — a check that ends
    in failure still fails the gate with the existing message, so the
    release keeps failing fast with the full evidence."""
    pages = [
        [("tests", "queued", None)],
        [("tests", "completed", "failure")],
    ]

    def fake_run_command(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            page = pages.pop(0)
            if not pages:
                pages.append(page)
            return json.dumps([
                {"name": n, "status": s, "conclusion": c}
                for n, s, c in page
            ])
        return "[]"

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match="check 'tests' is completed/failure"):
        release.check_release_gates("o/r", "abc123", 99)


def test_check_release_gates_ci_wait_times_out(monkeypatch, caplog):
    """Issue #268: exceeding the wait limit fails with its own reason —
    explicitly distinct from a CI failure."""
    def fake_run_command(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return json.dumps([
                {"name": "tests", "status": "in_progress",
                 "conclusion": None},
            ])
        return "[]"

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"), pytest.raises(
        failure.ReleaseGateWaitTimeout,
        match=r"waiting for CI on the release commit abc123 "
              r"timed out after 7s.*not a CI failure",
    ):
        release.check_release_gates("o/r", "abc123", 99,
                                   ci_wait_seconds=7.0)
    # The deadline is exact even when the poll interval exceeds the
    # remaining budget (the sleep step is capped, never overshot).
    assert "release_waiting_ci" in caplog.text


def test_check_release_gates_reports_missing_checks_permission(monkeypatch):
    """A hosted App token's Checks permission failure is actionable.

    The 403 is read from the response status gh printed (`--include`),
    never from the error text (Issue #1760)."""
    from orbi import gh_response

    def fake_run_command(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            raise gh_response.GitHubRequestFailed(
                1, command, status=403,
                stderr="gh: Resource not accessible by integration (HTTP 403)",
            )
        return "[]"

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    # Its own type (Issue #1760): the status meaning travels on the type,
    # so the classification still chooses the credential repair now that
    # the broad "credential" text marker is gone.
    with pytest.raises(
        failure.GitHubPermissionDenied, match=r"lacks Checks:read",
    ):
        release.check_release_gates("o/r", "abc123", 99)


def test_check_release_gates_reraises_real_gh_failure(monkeypatch):
    make_gate_gh(monkeypatch, check_runs=None)
    with pytest.raises(subprocess.CalledProcessError):
        release.check_release_gates("o/r", "abc123", 99)


def test_check_release_gates_scopes_leftover_scan_to_the_milestone(monkeypatch):
    """Issue #671: the leftover-delivery scan is limited to the release's
    Milestone through the documented `gh issue list --milestone` flag, and
    the evidence names the scope."""
    calls = make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [7, 8]},
        leftover_milestones={7: "v0.4.4", 8: None},
        check_runs=[],
    )
    evidence, repo_has_ci = release.check_release_gates(
        "o/r", "abc123", 99, milestone="v0.4.3",
    )
    assert evidence == [
        "no open Issue in milestone 'v0.4.3' carries "
        "ai-in-progress / ai-pr-opened / ai-fix-needed",
        # Issue #657: no check runs at all is recorded as evidence; the
        # repo_has_ci flag decides whether that is a pass or a wait.
        "CI on the release commit: no check runs on abc123 (nothing to "
        "gate)",
    ]
    assert repo_has_ci is False  # no check runs seen anywhere -> not proven
    scans = [c for c in calls if c[:3] == ["gh", "issue", "list"]]
    assert scans and all(
        c[c.index("--milestone") + 1] == "v0.4.3" for c in scans
    )


def test_check_release_gates_ignores_leftover_outside_the_milestone(monkeypatch):
    """Issue #671 (the v0.4.3 scene): an unrelated in-flight ticket with no
    Milestone no longer blocks the release."""
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [657, 658]},
        leftover_milestones={657: None, 658: None},
        check_runs=[],
    )
    evidence, _ = release.check_release_gates(
        "o/r", "abc123", 99, milestone="v0.4.3",
    )
    assert evidence[0].startswith("no open Issue in milestone 'v0.4.3'")


def test_check_release_gates_still_waits_on_leftover_in_the_milestone(monkeypatch):
    """Issue #671: the wait behavior is unchanged for in-flight tickets that
    DO belong to the released Milestone."""
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [7, 8]},
        leftover_milestones={7: "v0.4.3", 8: "v0.4.4"},
        check_runs=[],
    )
    with pytest.raises(runner.ReleaseDeliveriesWaiting) as excinfo:
        release.check_release_gates(
            "o/r", "abc123", 99, milestone="v0.4.3",
        )
    assert excinfo.value.issue_numbers == [7]


def test_check_release_gates_without_milestone_keeps_full_repo_scan(monkeypatch):
    """Issue #671: without a Milestone there is nothing to scope by — the
    pre-#671 full-repo scan is unchanged (never a `--milestone` flag)."""
    calls = make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [7]},
        check_runs=[],
    )
    with pytest.raises(runner.ReleaseDeliveriesWaiting):
        release.check_release_gates("o/r", "abc123", 99)
    assert not any("--milestone" in c for c in calls)


def test_run_release_tests_path_is_gone():
    """Issue #569: the release state machine has NO local test
    execution point — the declared test command is never run locally;
    test acceptance is the GitHub Actions CI result on the release
    commit."""
    assert not hasattr(runner, "run_release_tests")
    assert not hasattr(runner, "create_repair_issue")
    assert not hasattr(runner, "release_test_evidence")
    assert not hasattr(runner, "RELEASE_TEST_TIMEOUT_SECONDS")


def test_process_release_parse_failure_publishes_final_comment(
    monkeypatch, tmp_path, caplog,
):
    """Issue #474: a release that fails BEFORE `publisher.ensure` (the
    body is missing the `## Release` section) still publishes the final
    progress comment. Before the fix `finish()` raised
    `RuntimeError: no progress comment to update`, `_safe_publish`
    logged a `progress_publish_failed` traceback, and the final comment
    never landed.

    Driven through the real dispatch entry `process_issue`, so the
    release routing and the failure path are the production ones.
    """
    gh_calls, posted = make_fake_gh(monkeypatch)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    # The run id is a journal module global: binding the real one here
    # would prefix every later module's journal line, and this domain
    # suite runs before the bootstrap file's unprefixed-line scenes.
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    issue = {
        "number": 467,
        "title": "Release v0.4.2",
        "body": "this body has no release section",
        "labels": [{"name": "ai-release"}],
    }
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    with caplog.at_level("INFO"):
        result = runner.process_issue(issue, config, "orbi-build/orbi")
    assert result == runner.IssueResult("release", "")
    # The handled failure is terminal: `ai-blocked` ALONE.
    edits = [c for c in gh_calls if c[:3] == ["gh", "issue", "edit"]]
    assert edits == [
        ["gh", "issue", "edit", "467", "--repo", "orbi-build/orbi",
         "--add-label", "ai-in-progress"],
        ["gh", "issue", "edit", "467", "--repo", "orbi-build/orbi",
         "--add-label", "ai-blocked", "--remove-label", "ai-in-progress"],
    ]
    # The concrete failure scene is on the Issue.
    comments = [c for c in gh_calls if c[:3] == ["gh", "issue", "comment"]]
    assert any(
        "Orbi release failed (ai-blocked)" in c[-1] for c in comments
    )
    # Issue #474: the final progress comment is created even though
    # `ensure` never ran.
    assert any("**Orbi progress**" in body for body in posted)
    assert not any(
        "progress_publish_failed" in record.message
        for record in caplog.records
    )
    assert "no progress comment to update" not in caplog.text


def test_prepare_release_version_updates_sources_and_commits(tmp_path, monkeypatch):
    work = tmp_path / "release"
    work.mkdir()
    (work / "pyproject.toml").write_text(
        '[project]\nname = "orbi"\nversion = "0.2.0"\n',
        encoding="utf-8",
    )
    package = work / "src" / "orbi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '__version__ = "0.2.0"\n', encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "newsha",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "newsha")

    assert release.prepare_release_version(
        work, "v0.3.0", "main",
    ) == "newsha"
    assert 'version = "0.3.0"' in (work / "pyproject.toml").read_text()
    assert '__version__ = "0.3.0"' in (package / "__init__.py").read_text()
    assert calls[0] == (["git", "add", "pyproject.toml", "src/orbi/__init__.py"],
                        {"cwd": work})
    commit_call = calls[1]
    assert commit_call[0] == ["git", "commit", "-m", "chore: prepare release v0.3.0"]
    assert {key: commit_call[1]["env"][key] for key in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    )} == {
        "GIT_AUTHOR_NAME": "orbi-build[bot]",
        "GIT_AUTHOR_EMAIL": "327503608+orbi-build[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "orbi-build[bot]",
        "GIT_COMMITTER_EMAIL": "327503608+orbi-build[bot]@users.noreply.github.com",
    }
    assert calls[2] == (["git", "push", "origin", "HEAD:refs/heads/main"], {
        "cwd": work, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    })
    assert calls[3] == (["git", "rev-parse", "HEAD"], {"cwd": work})


def test_prepare_release_version_updates_pyproject_without_orbi_init(
    tmp_path, monkeypatch,
):
    """A non-Orbi repo has no src/orbi/__init__.py (Issue #1461)."""
    work = tmp_path / "release"
    work.mkdir()
    (work / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "newsha",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "newsha")

    assert release.prepare_release_version(
        work, "v0.1.0", "main",
    ) == "newsha"
    assert 'version = "0.1.0"' in (work / "pyproject.toml").read_text()
    assert calls[0] == (["git", "add", "pyproject.toml"], {"cwd": work})
    commit_call = calls[1]
    assert commit_call[0] == [
        "git", "commit", "-m", "chore: prepare release v0.1.0",
    ]
    assert calls[2] == (["git", "push", "origin", "HEAD:refs/heads/main"], {
        "cwd": work, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    })
    assert calls[3] == (["git", "rev-parse", "HEAD"], {"cwd": work})


def test_prepare_release_version_updates_package_json(tmp_path, monkeypatch):
    work = tmp_path / "release"
    work.mkdir()
    (work / "package.json").write_text(
        '{\n  "name": "cloud",\n  "version": "0.2.0"\n}\n', encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "newsha",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "newsha")

    assert release.prepare_release_version(
        work, "v0.3.0", "main", "package.json",
    ) == "newsha"
    assert '"version": "0.3.0"' in (work / "package.json").read_text()
    assert calls[0] == (["git", "add", "package.json"], {"cwd": work})
    commit_call = calls[1]
    assert commit_call[0] == ["git", "commit", "-m", "chore: prepare release v0.3.0"]
    assert commit_call[1]["env"]["GIT_COMMITTER_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert commit_call[1]["env"]["GIT_AUTHOR_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert calls[2] == (["git", "push", "origin", "HEAD:refs/heads/main"], {
        "cwd": work, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    })
    assert calls[3] == (["git", "rev-parse", "HEAD"], {"cwd": work})


@pytest.mark.parametrize(
    ("version_file", "source", "expected"),
    [
        (
            "pom.xml",
            "<project><parent><version>9.9.9</version></parent>"
            "<version>0.2.0</version><dependencies>"
            "<dependency><version>8.8.8</version></dependency>"
            "</dependencies></project>",
            "<version>0.3.0</version>",
        ),
        ("build.gradle", "version = '0.2.0'\n", "version = '0.3.0'"),
        ("build.gradle.kts", "version = \"0.2.0\"\n", "version = \"0.3.0\""),
        ("gradle.properties", "version=0.2.0\n", "version=0.3.0"),
        (
            "Cargo.toml",
            "[package]\nname = \"demo\"\nversion = \"0.2.0\"\n",
            'version = "0.3.0"',
        ),
        ("composer.json", '{"name":"demo","version":"0.2.0"}\n',
         '"version": "0.3.0"'),
        ("pubspec.yaml", "name: demo\nversion: 0.2.0\n", "version: 0.3.0"),
    ],
)
def test_prepare_release_version_updates_ecosystem_file(
    tmp_path, monkeypatch, version_file, source, expected,
):
    work = tmp_path / "release"
    work.mkdir()
    (work / version_file).write_text(source, encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "newsha",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "newsha")

    assert release.prepare_release_version(
        work, "v0.3.0", "main", version_file,
    ) == "newsha"
    updated = (work / version_file).read_text(encoding="utf-8")
    assert expected in updated
    if version_file == "Cargo.toml":
        assert "[package]" in updated
    assert calls[0] == (["git", "add", version_file], {"cwd": work})
    assert calls[-1] == (["git", "rev-parse", "HEAD"], {"cwd": work})


def test_prepare_release_version_ecosystem_file_is_idempotent(tmp_path, monkeypatch):
    work = tmp_path / "release"
    work.mkdir()
    (work / "gradle.properties").write_text("version=0.3.0\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "head",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "head")
    assert release.prepare_release_version(
        work, "v0.3.0", "main", "gradle.properties",
    ) == "head"
    assert calls == [(["git", "rev-parse", "HEAD"], {"cwd": work})]


def test_prepare_release_version_rejects_unparseable_ecosystem_file(tmp_path):
    work = tmp_path / "release"
    work.mkdir()
    (work / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="build.gradle"):
        release.prepare_release_version(work, "v0.3.0", "main", "build.gradle")


def test_prepare_release_version_rejects_cargo_without_package_version(tmp_path):
    work = tmp_path / "release"
    work.mkdir()
    (work / "Cargo.toml").write_text("[package]\nname = \"demo\"\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cargo.toml"):
        release.prepare_release_version(work, "v0.3.0", "main", "Cargo.toml")


def test_prepare_release_version_none_does_not_require_a_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "head",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "head")
    assert release.prepare_release_version(tmp_path, "v0.3.0", "main", "none") == "head"
    assert calls == [(["git", "rev-parse", "HEAD"], {"cwd": tmp_path})]


def test_prepare_release_version_rejects_unrecognized_file(tmp_path):
    with pytest.raises(ValueError, match="version_file"):
        release.prepare_release_version(tmp_path, "v0.3.0", "main", "VERSION")


def test_prepare_release_version_package_json_is_idempotent(tmp_path, monkeypatch):
    work = tmp_path / "release"
    work.mkdir()
    (work / "package.json").write_text(
        '{"name": "cloud", "version": "0.3.0"}\n', encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "head",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "head")

    assert release.prepare_release_version(
        work, "v0.3.0", "main", "package.json",
    ) == "head"
    assert calls == [(["git", "rev-parse", "HEAD"], {"cwd": work})]


def test_prepare_release_version_rejects_invalid_version_file(tmp_path):
    with pytest.raises(ValueError, match="version_file"):
        release.prepare_release_version(tmp_path, "v0.3.0", "main", "version.txt")


def test_prepare_release_version_rejects_invalid_package_json(tmp_path):
    work = tmp_path / "release"
    work.mkdir()
    (work / "package.json").write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        release.prepare_release_version(work, "v0.3.0", "main", "package.json")


def test_prepare_release_version_rejects_package_without_version(tmp_path):
    work = tmp_path / "release"
    work.mkdir()
    (work / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="version field"):
        release.prepare_release_version(work, "v0.3.0", "main", "package.json")


def test_prepare_release_version_none_does_not_change_files(tmp_path, monkeypatch):
    work = tmp_path / "release"
    work.mkdir()
    marker = work / "README.md"
    marker.write_text("unchanged\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "head",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "head")

    assert release.prepare_release_version(work, "v0.3.0", "main", "none") == "head"
    assert marker.read_text() == "unchanged\n"
    assert calls == [(["git", "rev-parse", "HEAD"], {"cwd": work})]


def test_prepare_release_version_rejects_non_tag_version(tmp_path):
    with pytest.raises(ValueError, match="release version"):
        release.prepare_release_version(tmp_path, "0.3.0", "main")


def test_prepare_release_version_rejects_disagreeing_sources(tmp_path):
    work = tmp_path / "release"
    package = work / "src" / "orbi"
    package.mkdir(parents=True)
    (work / "pyproject.toml").write_text(
        '[project]\nversion = "0.2.0"\n', encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="sources disagree"):
        release.prepare_release_version(work, "v0.3.0", "main")


def test_prepare_release_version_rejects_missing_source_declaration(tmp_path):
    work = tmp_path / "release"
    package = work / "src" / "orbi"
    package.mkdir(parents=True)
    (work / "pyproject.toml").write_text('[project]\n', encoding="utf-8")
    (package / "__init__.py").write_text(
        '__version__ = "0.2.0"\n', encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="exactly one"):
        release.prepare_release_version(work, "v0.3.0", "main")


def test_prepare_release_version_is_idempotent(tmp_path, monkeypatch):
    work = tmp_path / "release"
    package = work / "src" / "orbi"
    package.mkdir(parents=True)
    (work / "pyproject.toml").write_text(
        '[project]\nversion = "0.3.0"\n', encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        '__version__ = "0.3.0"\n', encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "head",
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "head")
    assert release.prepare_release_version(work, "v0.3.0", "main") == "head"
    assert calls == [(["git", "rev-parse", "HEAD"], {"cwd": work})]


def test_prepare_release_version_yields_when_concurrent_instance_won(
    tmp_path, monkeypatch,
):
    """Issue #1289: the loser of the version-push race fetches, proves the
    remote head content-equivalent to its own version commit, and yields
    instead of failing the release ticket."""
    winner, loser = make_release_race_clones(tmp_path)
    # The two independent instances commit at different wall-clock times,
    # so the identical content yields two distinct commit SHAs and the
    # loser's plain push really is rejected.
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    winner_commit = release.prepare_release_version(winner, "v0.3.0", "main")
    winner_tree = _git_output(winner, "rev-parse", "HEAD^{tree}")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T01:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T01:00:00")

    with pytest.raises(release.ReleaseVersionAlreadyLanded) as excinfo:
        release.prepare_release_version(
            loser, "v0.3.0", "main", repo_dir=loser,
        )

    assert excinfo.value.tag == "v0.3.0"
    assert excinfo.value.base_branch == "main"
    assert excinfo.value.remote_commit == winner_commit
    # The rejected push carried the same content; the fetch updated the
    # shared remote-tracking ref to the landed commit.
    assert _git_output(loser, "rev-parse", "HEAD^{tree}") == winner_tree
    assert _git_output(loser, "rev-parse", "origin/main") == winner_commit


def test_prepare_release_version_package_json_yields_when_instance_won(
    tmp_path, monkeypatch,
):
    """Issue #1289: the `package.json`/`composer.json` push site is the same
    race as the pyproject path — the loser yields on the content-equivalent
    remote head instead of failing the release ticket."""
    winner, loser = make_release_race_clones(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    winner_commit = release.prepare_release_version(
        winner, "v0.3.0", "main", "package.json",
    )
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T01:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T01:00:00")

    with pytest.raises(release.ReleaseVersionAlreadyLanded) as excinfo:
        release.prepare_release_version(
            loser, "v0.3.0", "main", "package.json", repo_dir=loser,
        )

    assert excinfo.value.remote_commit == winner_commit


def test_prepare_release_version_rejects_unrelated_remote_commit(tmp_path):
    """Issue #1289 failure path: the push is rejected and the remote head is
    unrelated, so the original push failure is re-raised unchanged."""
    winner, loser = make_release_race_clones(tmp_path)
    (winner / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git_output(winner, "add", "unrelated.txt")
    _git_output(winner, "commit", "-m", "unrelated change")
    _git_output(winner, "push", "origin", "main")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        release.prepare_release_version(
            loser, "v0.3.0", "main", repo_dir=loser,
        )

    assert "rejected" in (excinfo.value.stderr or "")


def test_push_prepared_release_version_reraises_a_plain_push_failure(
    tmp_path, monkeypatch,
):
    """Issue #1289: a push failing for any reason other than an advanced
    remote ref is not the race — it propagates unchanged, without fetching
    or second-guessing the failure."""
    calls = []

    def broken_push(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="fatal: unable to access the remote",
        )

    monkeypatch.setattr(seam, "run_git_network_command", broken_push)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        release_git.push_prepared_release_version(
            tmp_path, "v0.3.0", "main", tmp_path,
        )
    assert "unable to access" in (excinfo.value.stderr or "")
    assert calls == [["git", "push", "origin", "HEAD:refs/heads/main"]]


def test_release_tag_commit_returns_none_for_missing_remote_tag(tmp_path):
    work, _ = make_local_remote_pair(tmp_path)
    assert release.release_tag_commit(work, "v9.9.9") is None


def test_release_tag_commit_returns_the_tagged_commit(tmp_path):
    work, head = make_local_remote_pair(tmp_path)
    subprocess.run(
        ["git", "-C", str(work), "tag", "-a", "v0.1.0", "-m", "rel", head],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "v0.1.0"],
        check=True, capture_output=True,
    )
    assert release.release_tag_commit(work, "v0.1.0") == head


def test_release_tag_commit_reraises_real_fetch_failure(tmp_path, monkeypatch):
    work, _ = make_local_remote_pair(tmp_path)
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")
    monkeypatch.setattr(seam, "run_command", fail)
    monkeypatch.setattr(seam, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.release_tag_commit(work, "v0.1.0")


def test_release_tag_commit_never_reads_a_network_failure_as_missing_tag(
    tmp_path, monkeypatch,
):
    """fetch 的 exit 128 分不清"远端没有这个 tag"和"网络/鉴权故障"（对
    真实 CLI 实测两种都是 128）：把网络故障读成"缺失"会让流程走到重打
    tag 的分支，叠加本地残留就是发布票死锁（Issue #585）。改用 ls-remote
    后二者语义分离——exit 0 且空输出才是真缺失，任何非零退出原样抛出。"""
    work, _ = make_local_remote_pair(tmp_path)

    def fail_ls_remote(command, **kwargs):
        assert command[1] == "ls-remote"
        raise subprocess.CalledProcessError(128, command, stderr="boom")

    monkeypatch.setattr(seam, "run_git_network_command", fail_ls_remote)
    monkeypatch.setattr(seam, "run_git_network_command", fail_ls_remote)
    with pytest.raises(subprocess.CalledProcessError):
        release.release_tag_commit(work, "v0.1.0")


def test_release_git_writes_use_identity_without_git_config(tmp_path, monkeypatch):
    """Cloud-like empty Git config still yields attributable tag and commit."""
    # An in-process `runner.main()` pins the bot identity in `os.environ`
    # (Issue #1416), so a test that runs after one carries it too. Clear
    # it here: only then does this test prove the identity
    # `release_git.run_git_write` passes explicitly — without it, a
    # release fails with "Committer identity unknown".
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "release"
    work.mkdir()
    clean_env = {**os.environ, "HOME": str(tmp_path / "empty-home"),
                 "GIT_CONFIG_GLOBAL": "/dev/null"}
    (tmp_path / "empty-home").mkdir()
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True, env=clean_env)
    (work / "pyproject.toml").write_text(
        '[project]\nname = "orbi"\nversion = "0.2.0"\n', encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(work), "add", "pyproject.toml"],
                   check=True, capture_output=True, env=clean_env)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=seed",
         "-c", "user.email=seed@example.test", "commit", "-m", "seed"],
        check=True, capture_output=True, env=clean_env,
    )
    head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True,
                          env=clean_env).stdout.strip()
    monkeypatch.setenv("HOME", clean_env["HOME"])
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    release.ensure_release_tag_created(work, "v0.1.0", head)
    tagger = subprocess.run(
        ["git", "-C", str(work), "for-each-ref", "--format=%(taggername) %(taggeremail)",
         "refs/tags/v0.1.0"], check=True, capture_output=True, text=True,
        env=clean_env,
    ).stdout.strip()
    assert tagger == (
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>"
    )

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True, env=clean_env)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True, env=clean_env)
    package = work / "src" / "orbi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    release.prepare_release_version(work, "v0.3.0", "main")
    commit_identity = subprocess.run(
        ["git", "-C", str(work), "show", "-s", "--format=%an <%ae> %cn <%ce>"],
        check=True, capture_output=True, text=True, env=clean_env,
    ).stdout.strip()
    assert commit_identity == (
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com> "
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>"
    )


def test_ensure_release_tag_pushed_creates_and_pushes_when_no_local_tag(
    tmp_path, monkeypatch,
):
    work, head = make_local_remote_pair(tmp_path)
    pushed = []

    def fake_push(command, **kwargs):
        pushed.append(command)

    monkeypatch.setattr(seam, "run_git_network_command", fake_push)
    monkeypatch.setattr(seam, "run_git_network_command", fake_push)
    release.ensure_release_tag_pushed(work, "v0.1.0", head)
    tag_type = subprocess.run(
        ["git", "-C", str(work), "cat-file", "-t", "v0.1.0"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert tag_type == "tag"  # annotated, same as before
    assert pushed == [["git", "push", "origin", "refs/tags/v0.1.0"]]


def test_ensure_release_tag_pushed_repushes_local_residue(tmp_path, monkeypatch):
    """上次 `git tag` 成功但 push 失败会留下本地残留 tag：重试时远端
    仍无此 tag，不处理残留的话 `git tag -a` 永远 fatal: tag already
    exists（发布票死锁，Issue #585）。残留指向本次发布提交 → 直接重推
    收敛。"""
    work, head = make_local_remote_pair(tmp_path)
    subprocess.run(
        ["git", "-C", str(work), "tag", "-a", "v0.1.0", "-m", "rel", head],
        check=True, capture_output=True,
    )
    pushed = []

    def fake_push(command, **kwargs):
        pushed.append(command)

    monkeypatch.setattr(seam, "run_git_network_command", fake_push)
    monkeypatch.setattr(seam, "run_git_network_command", fake_push)
    release.ensure_release_tag_pushed(work, "v0.1.0", head)
    assert pushed == [["git", "push", "origin", "refs/tags/v0.1.0"]]


def test_ensure_release_tag_pushed_fails_fast_on_residue_pointing_elsewhere(
    tmp_path,
):
    work, head = make_local_remote_pair(tmp_path)
    subprocess.run(
        ["git", "-C", str(work), "commit", "--allow-empty",
         "-m", "other", "--quiet"],
        check=True, capture_output=True,
    )
    other = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(work), "tag", "-a", "v0.1.0", "-m", "rel", other],
        check=True, capture_output=True,
    )
    with pytest.raises(RuntimeError, match="never moved or overwritten"):
        release.ensure_release_tag_pushed(work, "v0.1.0", head)


def test_build_release_changelog_groups_descriptions_links_and_orders(monkeypatch):
    source = {
        30: {
            "__typename": "Issue", "number": 30,
            "title": "Deploy the exporter as a persistent service",
            "body": "The service keeps metrics available after restart.",
            "url": "https://github.com/o/r/issues/30",
            "stateReason": "COMPLETED",
            "labels": {"nodes": [{"name": "bug"}]},
            "closedByPullRequestsReferences": {"nodes": [{
                "number": 301, "url": "https://github.com/o/r/pull/301",
                "author": None,
            }]},
        },
        10: {
            "__typename": "Issue", "number": 10,
            "title": "Add Prometheus metrics dashboard",
            "body": "Users can inspect delivery metrics.",
            "url": "https://github.com/o/r/issues/10",
            "stateReason": "COMPLETED",
            "labels": {"nodes": [{"name": "enhancement"}]},
            "closedByPullRequestsReferences": {"nodes": []},
        },
        20: {
            "__typename": "Issue", "number": 20,
            "title": "Explain the full setup workflow",
            "body": "Documentation covers first use.",
            "url": "https://github.com/o/r/issues/20",
            "stateReason": "COMPLETED",
            "labels": {"nodes": [{"name": "documentation"}]},
            "closedByPullRequestsReferences": {"nodes": []},
        },
    }
    graphql_calls = []

    def fake_run_command(command, **kwargs):
        # Issue #1492: PR links are filtered by membership in the tagged
        # commit range, not by a per-PR state lookup; every closedBy PR
        # here is in range.
        assert command[:3] == ["gh", "api", "graphql"], command
        # Issue #772: author data rides the SAME per-Issue round trip —
        # no second query, and the variables travel as gh api graphql
        # -f/-F fields.
        assert next(a for a in command if a.startswith("owner=")) == "owner=o"
        assert next(a for a in command if a.startswith("name=")) == "name=r"
        graphql_calls.append(command)
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        return json.dumps({"data": {"repository": {"issue": source[number]}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [30, 10, 20], {301})
    assert changelog == """## Changelog

### Deployment and operations

- Deploy the exporter as a persistent service ([Issue #30](https://github.com/o/r/issues/30); [PR #301](https://github.com/o/r/pull/301))

### Observability

- Add Prometheus metrics dashboard ([Issue #10](https://github.com/o/r/issues/10))

### Documentation

- Explain the full setup workflow ([Issue #20](https://github.com/o/r/issues/20))"""
    # Exactly one GraphQL call per scope Issue, in scope order — and a
    # null author (PR #301) contributed no Contributors section.
    assert [next(a for a in c if a.startswith("number="))
            for c in graphql_calls] == ["number=30", "number=10", "number=20"]
    assert "## Contributors" not in changelog


def test_build_release_changelog_skips_not_planned_issues(monkeypatch):
    """Issue #707 (the v0.4.6 scene): #702 was closed NOT_PLANNED as a
    duplicate — it must not enter the Changelog, and its unmerged
    closedBy PR (#705) must not leak in through any entry, its author
    included (Issue #772)."""
    item = {
        "__typename": "Issue", "number": 702,
        "title": "Duplicate delivery ticket",
        "body": "Duplicate of #658",
        "url": "https://github.com/o/r/issues/702",
        "stateReason": "NOT_PLANNED",
        "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": [{
            "number": 705, "url": "https://github.com/o/r/pull/705",
            "author": {"login": "someone",
                       "avatarUrl": "https://avatars.githubusercontent.com/u/7?v=4"},
        }]},
    }

    def fake_run_command(command, **kwargs):
        # Any second command (e.g. a pr view) would fail this assert: a
        # NOT_PLANNED issue is skipped before its closedBy PRs are read.
        assert command[:3] == ["gh", "api", "graphql"], command
        return json.dumps({"data": {"repository": {"issue": item}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [702], {705})
    assert "#702" not in changelog
    assert "#705" not in changelog
    assert "Duplicate delivery ticket" not in changelog
    assert "someone" not in changelog
    assert "## Contributors" not in changelog


def test_build_release_changelog_omits_out_of_range_pr_links(monkeypatch):
    """Issue #1492: a closedBy PR link is written only when the PR is
    contained in the tagged commit range — a PR merged after the tag (or
    otherwise outside it) never appears in the release notes."""
    source = {
        10: {
            "__typename": "Issue", "number": 10,
            "title": "Ship the claim-race fix",
            "body": "detail",
            "url": "https://github.com/o/r/issues/10",
            "stateReason": "COMPLETED",
            "labels": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": [
                {"number": 11, "url": "https://github.com/o/r/pull/11",
                 "author": None},
                {"number": 705, "url": "https://github.com/o/r/pull/705",
                 "author": None},
            ]},
        },
    }
    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        return json.dumps({"data": {"repository": {"issue": source[10]}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [10], {11})
    assert "[PR #11](https://github.com/o/r/pull/11)" in changelog
    assert "#705" not in changelog


def test_build_release_changelog_lists_deduped_sorted_contributor_avatars(monkeypatch):
    """Issue #772: the notes end with a Contributors section — one
    32px avatar per merged closing-PR author, deduped across PRs,
    sorted by login, each linking to the author's GitHub profile. The
    avatar is fetched at s=48 (retina-sharp at the displayed size), and
    a null author neither crashes nor blocks the release. Issue #835:
    all avatars share ONE source line — each raw-HTML line renders as
    its own markdown block, so separate lines stacked the avatars
    vertically on the published release."""
    source = {
        10: {
            "__typename": "Issue", "number": 10, "title": "Add A",
            "body": "detail", "url": "https://github.com/o/r/issues/10",
            "stateReason": "COMPLETED", "labels": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": [
                {"number": 11, "url": "https://github.com/o/r/pull/11",
                 "author": {"login": "zzz",
                            "avatarUrl": "https://avatars.githubusercontent.com/u/1?v=4"}},
                {"number": 12, "url": "https://github.com/o/r/pull/12",
                 "author": {"login": "alice",
                            "avatarUrl": "https://avatars.githubusercontent.com/u/2?v=4"}},
                {"number": 13, "url": "https://github.com/o/r/pull/13",
                 "author": None},
            ]},
        },
        20: {
            "__typename": "Issue", "number": 20, "title": "Add B",
            "body": "detail", "url": "https://github.com/o/r/issues/20",
            "stateReason": "COMPLETED", "labels": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": [
                {"number": 21, "url": "https://github.com/o/r/pull/21",
                 "author": {"login": "zzz",
                            "avatarUrl": "https://avatars.githubusercontent.com/u/1?v=4"}},
                {"number": 22, "url": "https://github.com/o/r/pull/22",
                 "author": {"login": "bare",
                            "avatarUrl": "https://avatars.githubusercontent.com/u/9"}},
            ]},
        },
    }

    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        return json.dumps({"data": {"repository": {"issue": source[number]}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [10, 20], {11, 12, 13, 21, 22})
    assert changelog == """## Changelog

### Features

- Add A ([Issue #10](https://github.com/o/r/issues/10); [PR #11](https://github.com/o/r/pull/11); [PR #12](https://github.com/o/r/pull/12); [PR #13](https://github.com/o/r/pull/13))
- Add B ([Issue #20](https://github.com/o/r/issues/20); [PR #21](https://github.com/o/r/pull/21); [PR #22](https://github.com/o/r/pull/22))

## Contributors

<a href="https://github.com/alice"><img src="https://avatars.githubusercontent.com/u/2?v=4&s=48" width="32" height="32" alt="alice" /></a> <a href="https://github.com/bare"><img src="https://avatars.githubusercontent.com/u/9?s=48" width="32" height="32" alt="bare" /></a> <a href="https://github.com/zzz"><img src="https://avatars.githubusercontent.com/u/1?v=4&s=48" width="32" height="32" alt="zzz" /></a>"""


def test_build_release_changelog_without_contributors_has_no_section(monkeypatch):
    """Issue #772: every merged PR's author is null (ghosted account) —
    the release still succeeds and no empty `## Contributors` heading
    is written."""
    item = {
        "__typename": "Issue", "number": 10, "title": "Ship it",
        "body": "detail", "url": "https://github.com/o/r/issues/10",
        "stateReason": "COMPLETED", "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": [{
            "number": 11, "url": "https://github.com/o/r/pull/11",
            "author": None,
        }]},
    }

    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        return json.dumps({"data": {"repository": {"issue": item}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [10], {11})
    assert changelog == """## Changelog

### Features

- Ship it ([Issue #10](https://github.com/o/r/issues/10); [PR #11](https://github.com/o/r/pull/11))"""


def test_build_release_changelog_empty_scope_states_no_deliveries():
    """Issue #1384: a milestone with no closed Issue or merged PR produces
    a one-line empty-scope sentence instead of a header-only Changelog."""
    assert release.build_release_changelog("o/r", [], set()) == (
        "No deliveries are linked to this milestone."
    )


def test_build_release_changelog_out_of_range_pr_author_is_not_a_contributor(monkeypatch):
    """Issue #1492: contributors mirror the Changelog's PR links — a
    closedBy PR outside the tagged commit range never ships, so its
    author is not a contributor of this release."""
    item = {
        "__typename": "Issue", "number": 10, "title": "Ship it",
        "body": "detail", "url": "https://github.com/o/r/issues/10",
        "stateReason": "COMPLETED", "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": [
            {"number": 11, "url": "https://github.com/o/r/pull/11",
             "author": {"login": "alice",
                        "avatarUrl": "https://avatars.githubusercontent.com/u/2?v=4"}},
            {"number": 12, "url": "https://github.com/o/r/pull/12",
             "author": {"login": "bob",
                        "avatarUrl": "https://avatars.githubusercontent.com/u/3?v=4"}},
        ]},
    }
    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        return json.dumps({"data": {"repository": {"issue": item}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    changelog = release.build_release_changelog("o/r", [10], {11})
    assert "alice" in changelog
    assert "bob" not in changelog


def test_release_changelog_category_covers_reliability_bug_and_features():
    assert release.release_changelog_category({
        "title": "Recover a stalled delivery", "labels": [],
    }) == "Reliability and recovery"
    assert release.release_changelog_category({
        "title": "Fix an unrelated failure", "labels": [{"name": "bug"}],
    }) == "Bug fixes"
    assert release.release_changelog_category({
        "title": "Add a provider setting", "labels": "malformed",
    }) == "Features"


def test_build_release_changelog_fails_on_empty_or_malformed_evidence(monkeypatch):
    bad_issues = [
        {"__typename": "Issue", "number": 10, "title": "", "body": "", "url": "https://github.com/o/r/issues/10", "stateReason": "COMPLETED", "labels": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": []}},
        {"__typename": "Issue", "number": 10, "title": "Useful title", "body": "detail", "url": "not-a-url", "stateReason": "COMPLETED", "labels": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": []}},
        {"__typename": "Issue", "number": 10, "title": "Useful title", "body": "detail", "url": "https://github.com/o/r/issues/10", "stateReason": "COMPLETED", "labels": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": "malformed"}},
        {"__typename": "Issue", "number": 10, "title": "Useful title", "body": "detail", "url": "https://github.com/o/r/issues/10", "stateReason": "COMPLETED", "labels": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": [{"number": "bad", "url": "https://github.com/o/r/pull/20", "author": None}]}},
    ]
    for issue in bad_issues:
        monkeypatch.setattr(seam, "run_command",
            lambda *args, issue=issue, **kwargs: json.dumps(
                {"data": {"repository": {"issue": issue}}}),
        )
        monkeypatch.setattr(seam, "run_command",
            lambda *args, issue=issue, **kwargs: json.dumps(
                {"data": {"repository": {"issue": issue}}}),
        )
        with pytest.raises(ValueError, match="release changelog"):
            release.build_release_changelog("o/r", [10], {10})
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "data": {"repository": {"issue": {
            "__typename": "Issue", "number": 10, "title": "",
            "body": "\nConcrete summary\n",
            "url": "https://github.com/o/r/issues/10",
            "stateReason": "COMPLETED", "labels": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []},
        }}},
    }))
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "data": {"repository": {"issue": {
            "__typename": "Issue", "number": 10, "title": "",
            "body": "\nConcrete summary\n",
            "url": "https://github.com/o/r/issues/10",
            "stateReason": "COMPLETED", "labels": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []},
        }}},
    }))
    assert "Concrete summary" in release.build_release_changelog("o/r", [10], set())
    # A PR number in scope resolves through the PullRequest branch: no
    # stateReason, no closedBy references (verified against the real
    # GraphQL schema — the field only exists on Issue).
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "data": {"repository": {"issue": {
            "__typename": "PullRequest", "number": 10,
            "title": "Merge direct release work", "body": "",
            "url": "https://github.com/o/r/pull/10",
            "labels": {"nodes": []},
        }}},
    }))
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "data": {"repository": {"issue": {
            "__typename": "PullRequest", "number": 10,
            "title": "Merge direct release work", "body": "",
            "url": "https://github.com/o/r/pull/10",
            "labels": {"nodes": []},
        }}},
    }))
    assert "[PR #10](https://github.com/o/r/pull/10)" in (
        release.build_release_changelog("o/r", [10], {10})
    )


def test_release_range_prs_keeps_merged_in_range_prs_on_the_release_branch(
        tmp_path, monkeypatch):
    """Issue #1492: only PRs merged into the release branch and contained
    in `<previous v* tag>..<release commit>` count. A commit with no PR —
    a direct push, `chore: prepare release`, `chore: point
    active_milestone` — produces no entry, a PR targeting another branch
    is dropped, and an unmerged PR is dropped."""
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    squashed = commit_file(work, "squash.txt", "feat: squashed delivery")
    direct = commit_file(work, "direct.txt", "chore: prepare release")
    rebased = commit_file(work, "rebase.txt", "feat: rebased delivery")
    other_branch = commit_file(work, "other.txt", "feat: other branch")
    release_commit = commit_file(work, "chore.txt", "chore: point milestone")
    by_commit = {
        squashed: [{"number": 10, "merged_at": "2026-01-01T00:00:00Z",
                    "base": {"ref": "main"}}],
        direct: [],
        rebased: [
            {"number": 11, "merged_at": "2026-01-02T00:00:00Z",
             "base": {"ref": "main"}},
            {"number": 99, "merged_at": None, "base": {"ref": "main"}},
        ],
        other_branch: [{"number": 12, "merged_at": "2026-01-03T00:00:00Z",
                        "base": {"ref": "release"}}],
        release_commit: [],
    }
    calls = []
    mock_release_range_gh(monkeypatch, by_commit, calls)
    assert release.release_range_prs(
        work, "o/r", release_commit, "main",
    ) == {10, 11}
    # The range starts at the previous tag, not at the repository root.
    assert set(calls) == {
        squashed, direct, rebased, other_branch, release_commit,
    }


def test_previous_release_tag_ignores_a_side_branch_v_tag(tmp_path):
    """Issue #1492: `--first-parent` keeps a `v*` tag that lives only on a
    merged side branch from becoming the range's lower bound."""
    work = make_release_range_repo(tmp_path)
    base = commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    commit_file(work, "m1.txt", "feat: main one")
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "side", base],
                   check=True, capture_output=True)
    commit_file(work, "s1.txt", "feat: side one")
    git_out(work, "tag", "-a", "v0.2.5", "-m", "v0.2.5")
    subprocess.run(["git", "-C", str(work), "checkout", "main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "merge", "--no-ff", "-m",
                    "Merge side", "side"], check=True, capture_output=True)
    release_commit = commit_file(work, "m2.txt", "feat: main two")
    assert release.previous_release_tag(work, release_commit) == "v0.2.0"


def test_release_range_prs_without_a_previous_tag_covers_full_history(
        tmp_path, monkeypatch):
    """Issue #1492: the first release has no lower bound, so every
    first-parent commit is in range."""
    work = make_release_range_repo(tmp_path)
    first = commit_file(work, "a.txt", "feat: first delivery")
    second = commit_file(work, "b.txt", "feat: second delivery")
    by_commit = {
        first: [{"number": 10, "merged_at": "2026-01-01T00:00:00Z",
                 "base": {"ref": "main"}}],
        second: [{"number": 11, "merged_at": "2026-01-02T00:00:00Z",
                  "base": {"ref": "main"}}],
    }
    calls = []
    mock_release_range_gh(monkeypatch, by_commit, calls)
    assert release.release_range_prs(work, "o/r", second, "main") == {10, 11}
    assert set(calls) == {first, second}


def test_release_range_prs_defaults_to_the_current_branch(tmp_path, monkeypatch):
    """Issue #1492: without an explicit release branch, the checkout's
    current branch (`main`) is the base filter."""
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    sha = commit_file(work, "s.txt", "feat: delivery")
    mock_release_range_gh(monkeypatch, {sha: [
        {"number": 7, "merged_at": "2026-01-01T00:00:00Z",
         "base": {"ref": "main"}},
        {"number": 8, "merged_at": "2026-01-02T00:00:00Z",
         "base": {"ref": "other"}},
    ]})
    assert release.release_range_prs(work, "o/r", sha) == {7}


def test_release_range_prs_fails_fast_on_malformed_commit_evidence(
        tmp_path, monkeypatch):
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    sha = commit_file(work, "s.txt", "feat: delivery")
    mock_release_range_gh(monkeypatch, {sha: {"not": "a list"}})
    with pytest.raises(ValueError, match="malformed pull-request"):
        release.release_range_prs(work, "o/r", sha, "main")


def test_release_range_prs_skips_non_dict_and_baseless_entries(
        tmp_path, monkeypatch):
    """A malformed entry that cannot be a PR is skipped, never guessed;
    a PR without a base object is not on the release branch."""
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    sha = commit_file(work, "s.txt", "feat: delivery")
    mock_release_range_gh(monkeypatch, {sha: [
        "not-a-dict",
        {"number": 5, "merged_at": "2026-01-01T00:00:00Z"},
        {"number": 6, "merged_at": "2026-01-01T00:00:00Z",
         "base": "not-a-dict"},
    ]})
    assert release.release_range_prs(work, "o/r", sha, "main") == set()


def test_release_range_prs_fails_fast_on_malformed_pr_number(
        tmp_path, monkeypatch):
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    sha = commit_file(work, "s.txt", "feat: delivery")
    mock_release_range_gh(monkeypatch, {sha: [
        {"number": "bad", "merged_at": "2026-01-01T00:00:00Z",
         "base": {"ref": "main"}},
    ]})
    with pytest.raises(ValueError, match="number evidence"):
        release.release_range_prs(work, "o/r", sha, "main")


def test_previous_release_tag_treats_empty_describe_output_as_no_tag(
        monkeypatch):
    """A `git describe` that succeeds with empty output carries no tag
    name; the range has no lower bound."""
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    assert release.previous_release_tag(Path("/r"), "abc123") is None


def test_release_changelog_scope_maps_range_prs_to_closing_issues(monkeypatch):
    """Issue #1492: each range PR's closing Issues become Changelog items;
    a PR with no closing Issue is listed by its own PR number."""
    payloads = {
        10: {"__typename": "PullRequest", "number": 10,
             "closingIssuesReferences": {"nodes": [
                 {"number": 100}, {"number": 101}]}},
        11: {"__typename": "PullRequest", "number": 11,
             "closingIssuesReferences": {"nodes": []}},
    }

    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        return json.dumps({"data": {"repository": {
            "issueOrPullRequest": payloads[number]}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    assert release.release_changelog_scope("o/r", {10, 11}) == [11, 100, 101]


def test_release_changelog_scope_never_lists_the_release_issue(monkeypatch):
    """Issue #1492: the release Issue drives the release, it is not
    released work; a PR whose only closing Issue is that ticket falls
    back to its own PR number so no shipped work is lost."""
    payloads = {
        10: {"closingIssuesReferences": {"nodes": [{"number": 99}]}},
        11: {"closingIssuesReferences": {"nodes": [
            {"number": 200}, {"number": 99}]}},
    }

    def fake_run_command(command, **kwargs):
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        return json.dumps({"data": {"repository": {
            "issueOrPullRequest": payloads[number]}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    assert release.release_changelog_scope(
        "o/r", {10, 11}, release_issue=99,
    ) == [10, 200]


def test_release_changelog_scope_fails_on_malformed_closing_evidence(monkeypatch):
    def fake_run_command(command, **kwargs):
        return json.dumps({"data": {"repository": {"issueOrPullRequest": {
            "closingIssuesReferences": {"nodes": [{"title": "no number"}]},
        }}}})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    with pytest.raises(ValueError, match="closing-Issue evidence"):
        release.release_changelog_scope("o/r", {10})


def test_release_notes_exclude_post_tag_work_and_the_release_issue(
        tmp_path, monkeypatch):
    """Issue #1492 reproduction (orbi-cloud v0.6.89): a second run derived
    scope from the milestone against an advanced beta HEAD. The published
    notes listed #1564 (merged after the tag) and the release Issue #1555,
    and omitted PR #1551 (in the tag, never on the milestone)."""
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.6.88", "-m", "v0.6.88")
    shipped = commit_file(work, "shipped.txt", "feat: #1551")
    release_commit = commit_file(work, "version.txt", "chore: v0.6.89")
    post_tag = commit_file(work, "post.txt", "feat: #1564")
    by_commit = {
        shipped: [{"number": 1551, "merged_at": "2026-01-01T00:00:00Z",
                   "base": {"ref": "main"}}],
        release_commit: [],
        post_tag: [{"number": 1564, "merged_at": "2026-01-02T00:00:00Z",
                    "base": {"ref": "main"}}],
    }
    issues = {
        1551: {"__typename": "PullRequest", "number": 1551,
               "closingIssuesReferences": {"nodes": [{"number": 1500}]}},
        1500: {"__typename": "Issue", "number": 1500,
               "title": "Recover a stalled delivery",
               "body": "detail",
               "url": "https://github.com/o/r/issues/1500",
               "stateReason": "COMPLETED", "labels": {"nodes": []},
               "closedByPullRequestsReferences": {"nodes": [
                   {"number": 1551,
                    "url": "https://github.com/o/r/pull/1551",
                    "author": None}]}},
    }

    def fake_run_command(command, **kwargs):
        if command[0] == "gh" and command[1] == "api" and \
                command[2].startswith("repos/o/r/commits/") and \
                command[2].endswith("/pulls"):
            sha = command[2].split("/")[-2]
            return json.dumps(by_commit.get(sha, []))
        if command[:3] != ["gh", "api", "graphql"]:
            return real(command, **kwargs)
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        node = issues[number]
        return json.dumps({"data": {"repository": {
            "issueOrPullRequest": node, "issue": node}}})

    real = runner.run_command
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    range_prs = release.release_range_prs(work, "o/r", release_commit, "main")
    assert range_prs == {1551}
    scope = release.release_changelog_scope(
        "o/r", range_prs, release_issue=1555,
    )
    assert scope == [1500]
    changelog = release.build_release_changelog("o/r", scope, range_prs)
    assert "#1500" in changelog
    assert "[PR #1551]" in changelog
    assert "#1564" not in changelog
    assert "#1555" not in changelog


def test_release_notes_are_stable_when_a_second_run_recovers_the_tag(
        tmp_path, monkeypatch):
    """Issue #1492: after the base advanced past the tag, a resume
    recovers the tag commit as the release commit, recomputes identical
    notes, and an unchanged Release is never rewritten."""
    work = make_release_range_repo(tmp_path)
    commit_file(work, "base.txt", "base")
    git_out(work, "tag", "-a", "v0.6.88", "-m", "v0.6.88")
    shipped = commit_file(work, "shipped.txt", "feat: #1551")
    tag_commit = commit_file(work, "version.txt", "chore: v0.6.89")
    # A later commit landed on the base past the tag commit.
    commit_file(work, "later.txt", "chore: later commit")
    by_commit = {
        shipped: [{"number": 1551, "merged_at": "2026-01-01T00:00:00Z",
                   "base": {"ref": "main"}}],
        tag_commit: [],
    }
    issues = {
        1551: {"__typename": "PullRequest", "number": 1551,
               "closingIssuesReferences": {"nodes": [{"number": 1500}]}},
        1500: {"__typename": "Issue", "number": 1500,
               "title": "Recover a stalled delivery",
               "body": "detail",
               "url": "https://github.com/o/r/issues/1500",
               "stateReason": "COMPLETED", "labels": {"nodes": []},
               "closedByPullRequestsReferences": {"nodes": [
                   {"number": 1551,
                    "url": "https://github.com/o/r/pull/1551",
                    "author": None}]}},
    }

    def fake_run_command(command, **kwargs):
        if command[0] == "gh" and command[1] == "api" and \
                command[2].startswith("repos/o/r/commits/") and \
                command[2].endswith("/pulls"):
            sha = command[2].split("/")[-2]
            return json.dumps(by_commit.get(sha, []))
        if command[:3] != ["gh", "api", "graphql"]:
            return real(command, **kwargs)
        number = int(next(
            a for a in command if a.startswith("number=")
        ).split("=", 1)[1])
        node = issues[number]
        return json.dumps({"data": {"repository": {
            "issueOrPullRequest": node, "issue": node}}})

    real = runner.run_command
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    expected = None
    for _ in range(2):
        range_prs = release.release_range_prs(work, "o/r", tag_commit, "main")
        changelog = release.build_release_changelog(
            "o/r",
            release.release_changelog_scope(
                "o/r", range_prs, release_issue=1555,
            ),
            range_prs,
        )
        if expected is None:
            expected = changelog
        else:
            assert changelog == expected
    calls = make_release_gh(
        monkeypatch, release_exists=True, release_body=expected,
    )
    url = release.publish_release(
        repo="o/r", tag="v0.6.89", version="v0.6.89",
        release_commit=tag_commit, changelog=expected,
        scope_evidence=["PR #1551 merged"], gate_evidence=["gates passed"],
        test_evidence="tests passed", run_id="a1b2c3d4", issue_number=1555,
    )
    assert url == "https://github.com/o/r/releases/tag/v0.6.89"
    assert not [c for c in calls if c[:3] == ["gh", "release", "edit"]]


def test_publish_release_creates_when_missing_and_returns_url(monkeypatch):
    calls = make_release_gh(monkeypatch, release_exists=False)
    url = release.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog\n\n### Features\n\n- A useful change ([Issue #123](https://github.com/o/r/issues/123))",
        scope_evidence=["PR #123 merged (mergeCommit=aaa111)"],
        gate_evidence=["no open Issue carries ai-in-progress"],
        test_evidence="tests passed (exit 0)",
        run_id="a1b2c3d4", issue_number=99,
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    creates = [c for c in calls if c[:3] == ["gh", "release", "create"]]
    assert len(creates) == 1
    create = creates[0]
    assert create[:3] == ["gh", "release", "create"]
    assert create[3] == "v0.3.0"
    assert "--verify-tag" in create
    notes = create[create.index("--notes") + 1]
    assert "v0.3.0" in notes
    assert "## Changelog" in notes
    assert "A useful change" in notes
    assert "abc123" in notes
    assert "PR #123 merged (mergeCommit=aaa111)" in notes
    assert "no open Issue carries ai-in-progress" in notes
    assert "tests passed (exit 0)" in notes
    assert "<!-- orbi:run=a1b2c3d4 -->" in notes
    assert "run_id=a1b2c3d4" in notes
    assert "Issue #99" in notes
    assert notes.endswith(
        "Released by [Orbi](https://github.com/orbi-build/orbi)"
    )
    assert "orbi.build" not in notes
    assert "?ref=" not in notes


def test_publish_release_omits_attribution_when_disabled(monkeypatch):
    calls = make_release_gh(monkeypatch, release_exists=False)
    release.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99, attribution_footer=False,
    )
    create = next(c for c in calls if c[:3] == ["gh", "release", "create"])
    notes = create[create.index("--notes") + 1]
    assert "Released by Orbi" not in notes
    assert "orbi.build" not in notes
    assert "?ref=" not in notes


def test_publish_release_uses_the_configured_attribution_link(monkeypatch):
    """Issue #1537: the release footer carries exactly the configured link."""
    calls = make_release_gh(monkeypatch, release_exists=False)
    release.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99,
        attribution_link="https://example.com/x",
    )
    create = next(c for c in calls if c[:3] == ["gh", "release", "create"])
    notes = create[create.index("--notes") + 1]
    assert notes.endswith("Released by [Orbi](https://example.com/x)")
    assert "orbi.build" not in notes
    assert "?ref=" not in notes


def test_publish_release_reuses_the_existing_release(monkeypatch):
    calls = make_release_gh(monkeypatch, release_exists=True)
    url = release.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99,
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert not [c for c in calls if c[:3] == ["gh", "release", "create"]]
    assert not [c for c in calls if c[:3] == ["gh", "release", "edit"]]


def test_publish_release_upgrades_existing_notes_without_a_changelog(monkeypatch):
    calls = make_release_gh(
        monkeypatch, release_exists=True, release_body="# v0.2.0\n\nIssue #10 closed",
    )
    release.publish_release(
        repo="o/r", tag="v0.2.0", version="v0.2.0",
        release_commit="abc123", changelog="## Changelog\n\n### Features\n\n- Useful change",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99,
    )
    edits = [c for c in calls if c[:3] == ["gh", "release", "edit"]]
    assert len(edits) == 1
    assert edits[0][:6] == ["gh", "release", "edit", "v0.2.0", "--repo", "o/r"]
    assert "## Changelog" in edits[0][edits[0].index("--notes") + 1]


def test_publish_release_reraises_real_gh_failure(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 403: rate limited",
        )
    monkeypatch.setattr(seam, "run_command", fail)
    monkeypatch.setattr(seam, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.publish_release(
            repo="o/r", tag="v0.3.0", version="v0.3.0",
            release_commit="abc123", changelog="## Changelog",
            scope_evidence=[], gate_evidence=[], test_evidence="ok",
            run_id="a1b2c3d4", issue_number=99,
        )


def test_process_release_waiting_returns_ready_and_records_open_deliveries(monkeypatch):
    state = make_release_process_env(
        monkeypatch, leftover_labels={"ai-in-progress": [7], "ai-pr-opened": [8]},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-ready",
                                       "remove": "ai-in-progress"})
    waiting = [k["body"] for n, k in state["comments"]
               if n == 99 and "waiting for deliveries" in k["body"]]
    assert waiting and "open_deliveries: #7, #8" in waiting[0]


def test_process_release_ignores_unrelated_milestone_deliveries(monkeypatch):
    """Issue #671 acceptance (green): a no-Milestone in-flight ticket no
    longer blocks the release of Milestone v0.3.0 — the leftover scan is
    scoped through the documented `gh issue list --milestone` flag."""
    state = make_release_process_env(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [8]},
        leftover_milestones={8: None},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
             "milestone": {"title": "v0.3.0"}}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert not [
        k["body"] for n, k in state["comments"]
        if n == 99 and "waiting for deliveries" in k["body"]
    ]
    scans = [c for c, _ in state["commands"]
             if c[:3] == ["gh", "issue", "list"]]
    assert scans and all(
        c[c.index("--milestone") + 1] == "v0.3.0" for c in scans
    )


def test_process_release_still_waits_for_same_milestone_deliveries(monkeypatch):
    """Issue #671: an in-flight ticket of the released Milestone still
    blocks — the recoverable wait semantics are unchanged."""
    state = make_release_process_env(
        monkeypatch,
        leftover_labels={"ai-in-progress": [7]},
        leftover_milestones={7: "v0.3.0"},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
             "milestone": {"title": "v0.3.0"}}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    waiting = [k["body"] for n, k in state["comments"]
               if n == 99 and "waiting for deliveries" in k["body"]]
    assert waiting and "open_deliveries: #7" in waiting[0]


def test_process_release_wait_timeout_uses_persisted_wait_start(monkeypatch):
    state = make_release_process_env(
        monkeypatch, leftover_labels={"ai-in-progress": [7]},
    )
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [
        {
            "authorAssociation": "MEMBER",
            "body": "<!-- orbi:run=aaaaaaaa -->\n"
                    "Orbi release waiting for deliveries\n"
                    "wait_started: 90",
        },
        {
            "authorAssociation": "MEMBER",
            "body": "<!-- orbi:run=bbbbbbbb -->\n"
                    "Orbi release waiting for deliveries\n"
                    "wait_started: 95",
        },
        {"authorAssociation": "MEMBER", "body": "unrelated maintainer note"},
        {
            "authorAssociation": "MEMBER",
            "body": "Orbi release waiting for deliveries\nno timestamp",
        },
        {"authorAssociation": "NONE", "body": "wait_started: 0"},
        "not a comment",
    ])
    monkeypatch.setattr(runner.time, "time", lambda: 100)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main", release_deliveries_wait_seconds=5), "o/r",
    )
    assert result == ""
    assert state["edits"][-1][1]["add"] == "ai-blocked"


def test_process_release_uses_declared_package_version_file(monkeypatch):
    state = make_release_process_env(monkeypatch)
    seen = []
    monkeypatch.setattr(
        release, "prepare_release_version",
        lambda *args, **kwargs: seen.append(args) or "abc123",
    )
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n", "- version: v0.3.0\n- version_file: package.json\n",
    )
    issue = {"number": 99, "title": "Release v0.3.0", "body": body,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    release.process_release(issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r")
    assert seen == [(Path("/wt"), "v0.3.0", "main", "package.json")]


def test_process_release_blocks_at_claim_when_default_version_file_missing(monkeypatch):
    """Issue #740 (the orbi-cloud#246 scene): a TypeScript repo declares no
    `version_file`, the default `pyproject.toml` does not exist. The claim
    fails fast BEFORE the gates and the scope verification, and the
    failure comment names the missing file plus a supported file that
    exists and the `none` option."""
    state = make_release_process_env(
        monkeypatch, release_tree_files=["package.json"],
    )
    seen = []
    monkeypatch.setattr(
        release, "prepare_release_version",
        lambda *args, **kwargs: seen.append(args) or "abc123",
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    assert seen == []
    # Nothing ran between the frozen base and the check: no gate, no
    # scope verification, no version write.
    assert [c for c, _ in state["commands"]] == [
        ["git", "ls-tree", "--name-only", "abc123"],
    ]
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    failure = [k["body"] for n, k in state["comments"]
               if n == 99 and "ai-blocked" in k["body"]]
    assert failure and "pyproject.toml" in failure[0]
    assert "package.json" in failure[0]
    assert "version_file: none" in failure[0]


def test_process_release_blocks_when_declared_version_file_missing(monkeypatch):
    state = make_release_process_env(
        monkeypatch, release_tree_files=["pyproject.toml"],
    )
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0\n", "- version: v0.3.0\n- version_file: package.json\n",
    )
    issue = {"number": 99, "title": "Release v0.3.0", "body": body,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    failure = [k["body"] for n, k in state["comments"]
               if n == 99 and "ai-blocked" in k["body"]]
    assert failure and "package.json" in failure[0]


def test_process_release_yields_claim_when_version_already_landed(
    monkeypatch, caplog,
):
    """Issue #1289: a concurrent release instance already pushed the same
    version commit. The run yields — `claim_yield`, the ticket returns to
    the ready queue — and it is never rewritten `ai-blocked`."""
    state = make_release_process_env(
        monkeypatch,
        prepare_release_version_error=release.ReleaseVersionAlreadyLanded(
            "v0.3.0", "main", "abc123",
        ),
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        result = release.process_release(
            issue,
            config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"),
            "o/r",
        )
    assert result == ""
    # Yielded, not blocked: `ai-ready` replaces `ai-in-progress`.
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-ready",
                                       "remove": "ai-in-progress"})
    assert not any(
        edit[1].get("add") == "ai-blocked" for edit in state["edits"]
    )
    assert "claim_yield" in caplog.text
    assert "reason=release_version_already_landed" in caplog.text
    assert "remote_commit=abc123" in caplog.text
    yield_comments = [k["body"] for n, k in state["comments"]
                      if n == 99 and "yielded" in k["body"]]
    assert yield_comments
    # The yield comment must not seed the deliveries wait timer.
    assert "waiting for deliveries" not in yield_comments[0]


def test_process_release_blocks_when_version_push_rejected(monkeypatch, caplog):
    """Issue #1289 failure path: a rejected push whose remote head is not
    content-equivalent keeps the existing `ai-blocked` semantics, and no
    claim is yielded."""
    state = make_release_process_env(
        monkeypatch,
        prepare_release_version_error=subprocess.CalledProcessError(
            1, ["git", "push"], stderr="! [rejected] (fetch first)",
        ),
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        result = release.process_release(
            issue,
            config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"),
            "o/r",
        )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    assert "claim_yield" not in caplog.text


def test_process_release_success_end_to_end(monkeypatch):
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    # Claim first, terminal ai-merged at the end (ai-in-progress removed).
    assert state["edits"][0] == (99, {"repo": "o/r",
                                      "add": "ai-in-progress"})
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    # The tag was created at the release commit and pushed plainly.
    commands = [c for c, _ in state["commands"]]
    tag_calls = [item for item in state["commands"] if item[0][:3] == ["git", "tag", "-a"]]
    tag_call = next(item for item in tag_calls if item[0][-1] == "abc123")
    assert tag_call[0] == ["git", "tag", "-a", "v0.3.0", "-m", "Release v0.3.0", "abc123"]
    assert tag_call[1]["env"]["GIT_AUTHOR_NAME"] == "orbi-build[bot]"
    assert tag_call[1]["env"]["GIT_COMMITTER_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert (["git", "push", "origin", "refs/tags/v0.3.0"], {
        "cwd": Path("/r"),
        "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    }) in state["commands"]
    assert not any("--force" in c or "-f" == c for c in commands)
    # The release Issue is closed.
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    # Issue #214: the Milestone whose title is EXACTLY the released
    # version (v0.3.0 here) is closed via the official REST contract
    # AFTER the release Issue is closed — and no other Milestone is
    # touched.
    close_milestone = ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]
    assert close_milestone in commands
    assert commands.index(close_milestone) > commands.index(
        ["gh", "issue", "close", "99", "--repo", "o/r"])
    assert not [c for c in commands
                if c[:2] == ["gh", "api"] and "milestones/1" in c[2]]
    # Issue #569: no local test execution anywhere in the release run —
    # the `timeout`-wrapped declared command is gone; test acceptance is
    # the CI-wait gate evidence only.
    assert not [c for c, _ in state["commands"] if c[:1] == ["timeout"]]
    # The success comment carries the run marker and the release URL.
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]
    assert "run_id=a1b2c3d4" in comment_kwargs["body"]
    assert "https://github.com/o/r/releases/tag/v0.3.0" in comment_kwargs["body"]
    # Issue #811 regression: the post-gate comments keep the full field
    # set — the frozen branch and its commit stay on the success record
    # (rendered by `field_block` as `- key: value` lines).
    assert "- base_branch: main" in comment_kwargs["body"]
    assert "- base_sha: abc123" in comment_kwargs["body"]
    assert "PR #123 merged (mergeCommit=aaa111)" in comment_kwargs["body"]
    assert "Issue #124 closed" in comment_kwargs["body"]
    assert state["run_ids"][0] == "a1b2c3d4"


def test_release_run_pushes_no_docs_commit(monkeypatch):
    """Issue #1483 acceptance: a full release run performs no git commit
    and no remote write under `docs/` — the engine's release path never
    reads or writes the docs site (the repository's own
    `.github/workflows/release-docs.yml` owns the pages, Issue #1482)."""
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    assert release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    ) == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:2] == ["git", "commit"]]
    assert not [
        c for c in commands
        if c[:2] == ["git", "push"] and any("docs" in arg for arg in c)
    ]


def test_release_tag_push_leaves_a_docs_site_without_a_releases_group_alone(
        tmp_path):
    """Issue #1483 acceptance: on a repository whose `docs/docs.json` has
    no `Releases` navigation group, publishing the release tag succeeds
    without touching the docs site — the base branch does not move and no
    docs file changes (the repository's own workflow writes the pages)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True)
    work = make_release_range_repo(tmp_path)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin",
                    str(remote)], check=True, capture_output=True)
    (work / "docs").mkdir()
    commit_file(work, "docs/docs.json", "docs: site without a Releases group",
                content=json.dumps({
                    "name": "Orbi",
                    "navigation": {"languages": [{
                        "language": "en", "default": True,
                        "groups": [{"group": "Guides", "pages": ["start"]}],
                    }]},
                }))
    release_commit = commit_file(work, "version.txt", "chore: v0.3.0")
    base_head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "push", "origin", "main"],
                   check=True, capture_output=True)

    release.ensure_release_tag_pushed(work, "v0.3.0", release_commit)

    assert git_out(remote, "rev-parse", "refs/heads/main") == base_head
    assert git_out(work, "ls-remote", "origin", "refs/tags/v0.3.0").split()[0] == \
        git_out(work, "rev-parse", "refs/tags/v0.3.0")
    assert git_out(work, "status", "--porcelain") == ""
    assert "Releases" not in (work / "docs" / "docs.json").read_text(
        encoding="utf-8",
    )


def test_process_release_started_milestone_carries_base_branch(monkeypatch):
    """Issue #811: the `**Orbi release started**` milestone names the
    branch being frozen. The journal refactor deleted the middle
    `run_info` assignment, so the milestone fell back to the claim-time
    value (`run_id`/`priority` only) and a multi-branch repo's release
    notification could no longer prove which line it froze. The
    milestone is published BEFORE `freeze_base`, so it carries
    `base_branch` but not yet `base_sha`."""
    state = make_release_process_env(monkeypatch)

    def run_publish_actions(**kwargs):
        kwargs["action"]()

    monkeypatch.setattr(seam, "_safe_publish", run_publish_actions)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    started = [
        call.args[0]
        for call in state["publisher"].milestone.call_args_list
        if "Orbi release started" in call.args[0]
    ]
    assert started == [
        "**Orbi release started**: base_branch=main "
        "run_id=a1b2c3d4 priority=normal"
    ]


def test_process_release_status_comments_follow_the_message_language(
        monkeypatch,
):
    """Issue #1660: EVERY Runner-owned release status comment follows the
    bound message language — the started/gates/scope/tests/released
    milestones and the success record alike — while the machine facts
    (run marker, run_id, tag rows) stay verbatim."""
    state = make_release_process_env(monkeypatch)

    def run_publish_actions(**kwargs):
        # The real `_safe_publish` runs the action (bypass); run it here
        # so the milestone lambdas really render their text.
        kwargs["action"]()

    monkeypatch.setattr(seam, "_safe_publish", run_publish_actions)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue,
        config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            message_language="zh",
        ),
        "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    milestones = [
        call.args[0]
        for call in state["publisher"].milestone.call_args_list
    ]
    for rendered in (
        "**Orbi release 已开始**", "**Orbi release 门禁已通过**",
        "**Orbi release 发布范围已验证**", "**Orbi release 测试已通过**",
        "**Orbi 已发布**",
    ):
        assert any(rendered in text for text in milestones), rendered
    # No release milestone leaks the English wording.
    assert not any(
        "Orbi release scope verified" in text
        or "Orbi release tests passed" in text
        or "**Orbi released**" in text
        for text in milestones
    )
    # The success record is Chinese too; its machine rows stay verbatim.
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    body = comment_kwargs["body"]
    assert "Orbi 已发布：" in body
    assert "## 发布范围（逐项验证）" in body
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "run_id=a1b2c3d4" in body
    assert "https://github.com/o/r/releases/tag/v0.3.0" in body


def test_process_release_failure_comment_carries_base_branch(monkeypatch):
    """Issue #811: a release failing after the declaration parse (here:
    `freeze_base`) posts the terminal failure comment WITH the declared
    `base_branch` — the same middle `run_info` value the started
    milestone reads."""
    state = make_release_process_env(monkeypatch)

    def broken_freeze(repo_dir, base_branch):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(seam, "freeze_base", broken_freeze)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "Orbi release failed (ai-blocked)" in comment_kwargs["body"]
    assert "run_id=a1b2c3d4" in comment_kwargs["body"]
    assert "base_branch: main" in comment_kwargs["body"]
    assert "failure: freeze failed" in comment_kwargs["body"]


def test_process_release_failure_comment_carries_failure_block(monkeypatch):
    """Issue #1462: a failed release run posts the machine-readable
    `orbi:failure:v1` block, built by the SAME classifier the delivery
    path uses, directly under the run marker, so a status reader
    (Orbi Cloud) can classify the stop without reading prose."""
    state = make_release_process_env(monkeypatch)

    def broken_freeze(repo_dir, base_branch):
        raise RuntimeError("HTTP 401: bad credentials")

    monkeypatch.setattr(seam, "freeze_base", broken_freeze)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    body = comment_kwargs["body"]
    # The delivery path would classify this same cause the same way.
    expected = failure.classify(
        failure._failure_detail(RuntimeError("HTTP 401: bad credentials")),
    )
    assert failure.parse(body) == expected
    assert expected.reason_code in failure.REASON_CODES
    assert expected.action_code in failure.ACTION_CODES
    # Exactly one block, marker first, block directly under it.
    assert body.count("<!-- orbi:failure:v1") == 1
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=a1b2c3d4 -->"
    assert lines[1] == failure.render(expected)
    assert "Orbi release failed (ai-blocked)" in body


@pytest.mark.parametrize("budget", ["deliveries", "ci"])
def test_process_release_reports_a_gate_wait_timeout_as_blocked(
    monkeypatch, budget,
):
    """Issue #1760 acceptance 3: a release gate that runs out of its wait
    budget is reported `blocked` — a human decides — and never as the
    `github_transient` / `requeue` a transient GitHub failure gets."""
    if budget == "deliveries":
        state = make_release_process_env(
            monkeypatch, leftover_labels={"ai-in-progress": [7]},
        )
        config = config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            release_deliveries_wait_seconds=0.0,
        )
    else:
        state = make_release_process_env(
            monkeypatch,
            check_run_pages=[[("tests", "in_progress", None)]],
        )
        config = config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            release_ci_wait_seconds=0.0,
        )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    assert release.process_release(issue, config, "o/r") == ""
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    record = failure.parse(comment_kwargs["body"])
    assert record == failure.Failure(
        reason_code="human_decision_required", action_code="decide",
        retry_safe=False, outcome="blocked",
    )
    assert "timed out" in comment_kwargs["body"]


def test_process_release_reports_missing_checks_permission_as_a_credential_repair(
    monkeypatch,
):
    """Issue #1760: the release CI gate's 403 names the missing
    Checks:read scope in its sentence and carries the status meaning on
    its TYPE, so the machine block still says credential_missing /
    check_credentials after the broad "credential" text marker was
    dropped."""
    state = make_release_process_env(monkeypatch, check_runs_status=403)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    config = config_domain.RunnerConfig(
        repo_dir=Path("/r"), base_branch="main",
    )
    assert release.process_release(issue, config, "o/r") == ""
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "lacks Checks:read" in comment_kwargs["body"]
    record = failure.parse(comment_kwargs["body"])
    assert record == failure.Failure(
        reason_code="credential_missing", action_code="check_credentials",
        retry_safe=False, outcome="blocked",
    )


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_process_release_failure_comment_ends_with_the_help_line(
        monkeypatch, language_code):
    """Issue #1730: the terminal release-failure comment is a blocked
    comment, so it goes through the ONE blocked exit: the body keeps its
    reason, the machine block and the run info, and ends with the
    community help line in the run's language, exactly once."""
    state = make_release_process_env(monkeypatch)

    def broken_freeze(repo_dir, base_branch):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(seam, "freeze_base", broken_freeze)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            message_language=language_code,
        ), "o/r",
    )
    assert result == ""
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    body = comment_kwargs["body"]
    help_line = language.text(language_code, "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1
    assert "run_id=a1b2c3d4" in body
    assert "base_branch: main" in body
    assert "failure: freeze failed" in body
    assert body.count("<!-- orbi:failure:v1") == 1


def test_process_release_refreshes_deployment_cli_after_version_bump(
    monkeypatch, tmp_path,
):
    """Issue #490: a foreign Node source worktree is not Orbi's install input.
    Issue #569: after the CLI refresh the run goes straight to the
    tag step — no local test execution sits in between."""
    make_release_process_env(monkeypatch)
    source = tmp_path / "node-source"
    deployment = tmp_path / "orbi-deployment"
    source.mkdir()
    (source / "package.json").write_text("{}", encoding="utf-8")
    deployment.mkdir()
    (deployment / "pyproject.toml").write_text(
        "[project]\nname = 'orbi'\n", encoding="utf-8",
    )
    order = []
    monkeypatch.setattr(
        release, "prepare_release_version",
        lambda worktree, tag, base_branch, **kwargs: order.append("version") or "abc123",
    )

    def refresh(repo_dir, **kwargs):
        order.append("cli")
        assert Path(repo_dir) == deployment
        assert kwargs["lock_repo_dir"] == deployment
        assert kwargs["run_command"] is runner.run_command
        return "installed"

    monkeypatch.setattr(seam, "refresh_cli_install", refresh)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}

    assert release.process_release(
        issue,
        config_domain.RunnerConfig(repo_dir=source, deploy_home=deployment, base_branch="main"),
        "o/r",
    ) == "https://github.com/o/r/releases/tag/v0.3.0"
    assert order == ["version", "cli"]


def test_process_release_derives_scope_from_milestone(monkeypatch):
    state = make_release_process_env(
        monkeypatch,
        body=RELEASE_MILESTONE_DECLARATION_BODY,
        milestone_items={5: {
            "issues_closed": [
                {"number": 123, "title": "Deliver A", "state": "closed"},
                {"number": 124, "title": "Deliver B", "state": "closed"},
            ],
        }},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_MILESTONE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    # The scope was derived from the milestone, not hand-listed.
    assert ["gh", "api",
            "repos/o/r/issues?state=closed&milestone=5&per_page=100",
            "--paginate", "--slurp", "--include"] \
        in commands
    # The derived scope went through the existing item-by-item verify.
    (comment_number, comment_kwargs), = state["comments"]
    assert "PR #123 merged (mergeCommit=aaa111)" in comment_kwargs["body"]
    assert "Issue #124 closed" in comment_kwargs["body"]


def test_process_release_lists_open_milestone_items(monkeypatch, caplog):
    state = make_release_process_env(
        monkeypatch,
        body=RELEASE_MILESTONE_DECLARATION_BODY,
        milestone_items={5: {
            "issues_closed": [
                {"number": 123, "title": "Deliver A", "state": "closed"},
            ],
            "issues_open": [
                {"number": 255, "title": "Still open work",
                 "state": "open"},
            ],
        }},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_MILESTONE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with caplog.at_level("INFO"):
        url = release.process_release(
            issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
        )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    # Open items are surfaced as a warning and in the auditable success
    # comment — never silently swallowed, never part of the scope.
    warn_records = [
        record.message for record in caplog.records
        if "release_milestone_open_items" in record.message
    ]
    assert len(warn_records) == 1
    assert 'open_items="open Issue #255 Still open work"' in warn_records[0]
    (comment_number, comment_kwargs), = state["comments"]
    assert ("NOT released (still open in milestone v0.3.0): "
            "open Issue #255 Still open work") in comment_kwargs["body"]
    assert "PR #123 merged (mergeCommit=aaa111)" in comment_kwargs["body"]


def test_process_release_exempts_release_issue_from_open_evidence(monkeypatch):
    """Issue #818 regression: with `scope_from_milestone`, the release
    Issue itself is necessarily still open in its Milestone while the
    notes are generated (it closes only after the Release is published),
    so it must never surface as the self-referential "NOT released" line
    on the immutable Release page — this pins the caller's
    `release_issue=number` wiring end to end."""
    state = make_release_process_env(
        monkeypatch,
        body=RELEASE_MILESTONE_DECLARATION_BODY,
        milestone_items={5: {
            "issues_closed": [
                {"number": 123, "title": "Deliver A", "state": "closed"},
            ],
            "issues_open": [
                {"number": 99, "title": "Release v0.3.0", "state": "open"},
            ],
        }},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_MILESTONE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    # The release still completes terminally, and the auditable scope
    # evidence (the same list the Release notes carry) carries the
    # derived delivery but NO self-referential NOT released line.
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "NOT released" not in comment_kwargs["body"]
    assert "PR #123 merged (mergeCommit=aaa111)" in comment_kwargs["body"]


def test_process_release_proceeds_with_empty_derived_scope(monkeypatch):
    """Issue #1384: a Milestone with no deliveries must not block the
    release. The derived scope stays empty, the release notes carry the
    one-line sentence instead of a Changelog list, and the release
    completes (version bump, tag, GitHub Release, ai-merged close)."""
    state = make_release_process_env(
        monkeypatch,
        body=RELEASE_MILESTONE_DECLARATION_BODY,
        # Issue #1492: the notes come from the tagged commit range; an
        # empty range yields the same one-line empty-scope sentence.
        range_prs=set(),
        milestone_items={5: {
            "issues_open": [
                {"number": 255, "title": "Still open work",
                 "state": "open"},
            ],
        }},
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_MILESTONE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
             "milestone": {"title": "v0.3.0"}}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    # The release completes: terminal ai-merged, tag and Issue close.
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    commands = [c for c, _ in state["commands"]]
    assert [c for c in commands if c[:2] == ["git", "tag"]]
    assert [c for c in commands if c[:3] == ["gh", "issue", "close"]]
    # The one-line empty-scope sentence replaces the Changelog list in
    # the GitHub Release notes.
    empty_scope_changelog = "No deliveries are linked to this milestone."
    assert state["published"][0]["changelog"] == empty_scope_changelog


def test_process_release_waits_for_pending_ci_and_succeeds(monkeypatch):
    """Issue #268 regression (the 2026-09-04 v0.3.0 scene): the release
    always runs right after the last delivery PR merge, so the release
    commit's CI is almost certainly still `in_progress`. The state
    machine waits for the final conclusion and completes instead of
    ai-blocking; the wait is reflected in the live progress comment."""
    state = make_release_process_env(
        monkeypatch,
        check_run_pages=[[("tests", "in_progress", None)],
                         [("tests", "completed", "success")]],
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)

    def run_publish_actions(**kwargs):
        # The real `_safe_publish` runs the action; run it so the
        # progress-comment wait reflection is actually recorded.
        kwargs["action"]()

    monkeypatch.setattr(seam, "_safe_publish", run_publish_actions)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert not any(k.get("add") == "ai-blocked" for _, k in state["edits"])
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    publisher = release.ProgressPublisher.return_value
    patched = [c.args[0] for c in publisher.patch.call_args_list]
    assert any("waiting CI: check 'tests' is in_progress/None" in body
               for body in patched)


def test_process_release_ci_wait_timeout_blocks_with_distinct_reason(
        monkeypatch):
    """Issue #268: a CI wait that exceeds the configured limit blocks
    the release with the timeout reason — explicitly distinct from a CI
    failure."""
    state = make_release_process_env(
        monkeypatch,
        check_run_pages=[[("tests", "in_progress", None)]],
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue,
        config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main", release_ci_wait_seconds=0),
        "o/r",
    )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    (comment_number, comment_kwargs), = [
        (n, k) for n, k in state["comments"]
        if "Orbi release failed (ai-blocked)" in k["body"]
    ]
    assert comment_number == 99
    assert "waiting for CI on the release commit abc123 timed out after 0s" \
        in comment_kwargs["body"]
    assert "not a CI failure" in comment_kwargs["body"]


def test_process_release_gate_failure_blocks_and_returns_cleanly(monkeypatch):
    """A failed release gate is handled business failure, not a tick crash."""
    state = make_release_process_env(
        monkeypatch,
        check_run_pages=[[('tests', 'completed', 'failure')]],
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}

    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )

    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                        "remove": "ai-in-progress"})
    assert "CI check 'tests' is completed/failure" in (
        state["comments"][-1][1]["body"]
    )


def test_process_release_publish_closeout_failure_keeps_release_result(
    monkeypatch,
):
    """tag 和 GitHub Release 已发布之后，ai-merged 转移/关票都是既成事实的
    记账（Issue #79 旁路语义）：瞬时失败只记日志，绝不允许掉进通用异常
    处理器把已发布结果改写成 ai-blocked（仓库里同时挂 ai-merged 与
    ai-blocked 双终态）。"""
    state = make_release_process_env(monkeypatch)
    real_edit = runner.edit_issue

    def failing_edit(number, **kwargs):
        if kwargs.get("add") == "ai-merged":
            raise RuntimeError("GitHub unavailable")
        return real_edit(number, **kwargs)

    monkeypatch.setattr(seam, "edit_issue", failing_edit)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    # The published release result stands; the closeout failure never
    # rewrote it as blocked.
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert not any(k.get("add") == "ai-blocked" for _, k in state["edits"])
    assert any(
        "release_publish_closeout_failed" in str(call)
        for call in release.LOGGER.exception.call_args_list
    )


def test_process_release_success_comment_failure_keeps_release_result(
    monkeypatch,
):
    """成功评论同样只是既成发布的证据（Issue #79 旁路语义）：它失败只记
    `release_success_comment_failed` 日志，绝不允许把已发布结果改写成
    ai-blocked（Issue #601 的 CI diff 覆盖缺口正是这条路径）。"""
    state = make_release_process_env(monkeypatch)

    def failing_comment(number, **kwargs):
        # In the success path the ONLY comment_issue call is the success
        # comment itself (the milestone test asserts a single comment), so
        # failing unconditionally fails exactly that comment.
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "comment_issue", failing_comment)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    # The published release result stands; the failed evidence comment
    # never rewrote it as blocked.
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    assert not any(k.get("add") == "ai-blocked" for _, k in state["edits"])
    # The success comment never landed and its failure is logged.
    assert not state["comments"]
    assert any(
        "release_success_comment_failed" in str(call)
        for call in release.LOGGER.exception.call_args_list
    )


def test_process_release_milestone_failure_keeps_release_successful(monkeypatch):
    state = make_release_process_env(monkeypatch)
    real = runner.run_command

    def milestone_failing(command, **kwargs):
        # The v0.3.0 Milestone still has an open Issue: the close must
        # fail fast, but the already-published release remains successful.
        if command[:2] == ["gh", "api"] and "?state=all" in command[2]:
            return json.dumps([[
                {"number": 5, "title": "v0.3.0", "state": "open",
                 "open_issues": 1, "closed_issues": 2,
                 "url": "https://api.github.com/repos/o/r/milestones/5",
                 "html_url": "https://github.com/o/r/milestone/5"},
            ]])
        if command[:2] == ["gh", "api"] and "issues?milestone=5&state=open" in command[2]:
            return json.dumps([[{"number": 101, "title": "leftover"}]])
        return real(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", milestone_failing)
    monkeypatch.setattr(seam, "run_command", milestone_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    # The release itself succeeded (tag pushed, Release published,
    # Issue closed with ai-merged)...
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    assert any(k.get("add") == "ai-merged" for _, k in state["edits"])
    # ...but the Milestone close failed: no PATCH was issued. The failure
    # is retained as evidence in the success comment and does not rewrite
    # the terminal state.
    assert not [c for c in commands if "PATCH" in c]
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "Orbi released" in comment_kwargs["body"]
    # Issue #808: the wording states the third criterion's miss plainly —
    # the milestone was NOT closed — instead of the opaque
    # "milestone evidence unavailable".
    assert "milestone NOT closed" in comment_kwargs["body"]
    assert "Milestone #5" in comment_kwargs["body"]
    assert "#101" in comment_kwargs["body"]


def test_process_release_closes_milestone_despite_stale_release_ticket(monkeypatch):
    """Issue #808 全链路：成功路径刚关掉 release 票（Issue #99），milestone
    open 列表仍列出它自己（索引未刷新）——release 票被确定性排除，
    milestone 正常关闭，评论证据注明排除。v0.2.0 的随机失败就此消除。"""
    state = make_release_process_env(monkeypatch)
    harness = runner.run_command
    # Same full-command contract the seam-level tests pin
    # (MILESTONE_ISSUES_COMMAND); the dispatch matches the whole argv so
    # the patch-ratchet's command[:N] pattern (Issue #789) stays frozen.
    stale_index_command = [
        "gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100",
        "--paginate", "--slurp",
    ]

    def stale_release_ticket(command, **kwargs):
        if without_include(command) == stale_index_command:
            return json.dumps(
                [[{"number": 99, "title": "Release v0.3.0"}]],
            )
        return harness(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", stale_release_ticket)
    monkeypatch.setattr(seam, "run_command", stale_release_ticket)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    assert ["gh", "api", "repos/o/r/milestones/5",
            "--method", "PATCH", "-f", "state=closed"] in commands
    assert state["edits"][-2] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # Issue #1526: the release merge also clears the ready queue entry.
    assert state["edits"][-1] == (99, {"repo": "o/r", "remove": "ai-ready"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "closed after release v0.3.0" in comment_kwargs["body"]
    assert "release ticket #99 excluded" in comment_kwargs["body"]


def test_process_release_publish_failure_blocks_with_the_reason(monkeypatch):
    """The tag is published but the GitHub Release failed: the release is
    NOT reported successful — the terminal state is ai-blocked with the
    concrete reason on the Issue."""
    state = make_release_process_env(monkeypatch)
    monkeypatch.setattr(
        release, "publish_release",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("publish down")),
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    assert release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"),
        "o/r",
    ) == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    assert not any(k.get("add") == "ai-merged" for _, k in state["edits"])
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "Orbi release failed (ai-blocked)" in comment_kwargs["body"]
    assert "publish down" in comment_kwargs["body"]


def test_process_release_tag_push_failure_blocks(monkeypatch):
    state = make_release_process_env(monkeypatch)
    monkeypatch.setattr(
        release, "ensure_release_tag_pushed",
        lambda *args: (_ for _ in ()).throw(RuntimeError("tag push down")),
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    assert release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"),
        "o/r",
    ) == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "tag push down" in comment_kwargs["body"]


def test_process_release_reuses_the_run_id_on_resume(monkeypatch):
    state = make_release_process_env(
        monkeypatch, in_progress=True, existing_run_id="deadbeef",
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"},
                        {"name": "ai-in-progress"}]}
    release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    # The fresh id is generated first (the normal-path rule), then the
    # resumed run id wins — the terminal comment carries the resumed id.
    assert state["run_ids"] == ["a1b2c3d4", "deadbeef"]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=deadbeef -->" in comment_kwargs["body"]


def test_process_release_fails_on_malformed_declaration(monkeypatch):
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0", "body": "no section",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    # Terminal failure: ai-blocked ALONE, no ai-merged, no close.
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    assert not any(k.get("add") == "ai-merged" for _, k in state["edits"])
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:3] == ["gh", "issue", "close"]]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]
    assert "Milestone" in comment_kwargs["body"]


def test_process_release_fails_on_tag_mismatch_without_moving_it(monkeypatch):
    state = make_release_process_env(monkeypatch, tag_commit="other123")
    # A genuine tag mismatch: the tag commit is NOT an ancestor of the base
    # (the merge-base check fails), so the release must fail fast instead of
    # recovering the tag commit (Issue #275).
    real_run = runner.run_command

    def merge_base_failing(command, **kwargs):
        # Only the tag check's merge-base call fails (the tag commit
        # "other123" is not an ancestor of the base); the scope-verification
        # merge-base call (ancestor "aaa111") still succeeds.
        if command[:3] == ["git", "merge-base", "--is-ancestor"] \
                and command[3] == "other123":
            raise subprocess.CalledProcessError(
                1, command, stderr="not an ancestor",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", merge_base_failing)
    monkeypatch.setattr(seam, "run_command", merge_base_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:2] == ["git", "tag"]]
    assert not [c for c in commands if c[:2] == ["git", "push"]]
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "other123" in comment_kwargs["body"]
    assert "abc123" in comment_kwargs["body"]


def test_process_release_reuses_a_matching_existing_tag(monkeypatch):
    state = make_release_process_env(monkeypatch, tag_commit="abc123")
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:2] == ["git", "tag"]]
    assert not [c for c in commands if c[:2] == ["git", "push"]]


def test_process_release_recovers_existing_ancestor_tag(monkeypatch):
    """Issue #1492: the tagged commit range is computed against the release
    commit the tag-conflict/resume handling settled on, never the advanced
    base HEAD."""
    sink = []
    state = make_release_process_env(
        monkeypatch, tag_commit="tag123", range_commit_sink=sink,
    )
    monkeypatch.setattr(
        release, "tag_commit_is_ancestor_of_base", lambda *args: True,
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    assert release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    ) == "https://github.com/o/r/releases/tag/v0.3.0"
    assert state["published"][0]["release_commit"] == "tag123"
    assert sink == ["tag123"]


def test_process_release_fails_on_scope_violation(monkeypatch):
    # Reuse the env, but make scope item #123 an OPEN PR.
    state = make_release_process_env(monkeypatch)
    real = runner.run_command
    def scope_failing(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"] and command[3] == "123":
            return json.dumps({
                "number": 123, "state": "OPEN", "mergeCommit": None,
            })
        return real(command, **kwargs)
    monkeypatch.setattr(seam, "run_command", scope_failing)
    monkeypatch.setattr(seam, "run_command", scope_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})


def test_close_release_milestone_closes_an_open_empty_milestone(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[
                _milestone(4, "v0.2.0", "closed", 0),
                _milestone(5, "v0.3.0", "open", 0),
                _milestone(6, "v0.4.0", "open", 3),
            ]])
        if command == MILESTONE_ISSUES_COMMAND:
            return json.dumps([[]])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps(_milestone(5, "v0.3.0", "closed", 0))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    evidence = release.close_release_milestone("o/r", "v0.3.0")
    # The close uses the official REST contract
    # (PATCH /repos/{owner}/{repo}/milestones/{number}, state=closed)
    # and the list query asks for ALL states (the idempotent case needs
    # the closed Milestones too).
    assert calls[-1] == ["gh", "api", "repos/o/r/milestones/5",
                        "--method", "PATCH", "-f", "state=closed"]
    assert calls[0] == MILESTONE_LIST_COMMAND
    assert "Milestone #5" in evidence
    assert "https://github.com/o/r/milestone/5" in evidence
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_is_idempotent_when_already_closed(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[
                _milestone(1, "v0.2.0", "closed", 0),
                _milestone(2, "v0.3.0", "closed", 0),
            ]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    evidence = release.close_release_milestone("o/r", "v0.3.0")
    # Already closed: no mutation at all (no PATCH, no reopen).
    assert calls == [MILESTONE_LIST_COMMAND]
    assert "already closed" in evidence
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_without_a_matching_title(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[
                _milestone(1, "v0.2.0", "closed", 0),
                _milestone(3, "v0.4.0", "open", 7),
            ]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="no Milestone with the exact title"):
        release.close_release_milestone("o/r", "v0.3.0")
    # No mutation, no fuzzy match against a different Milestone.
    assert calls == [MILESTONE_LIST_COMMAND]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_when_open_issues_remain(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[
                _milestone(5, "v0.3.0", "open", 2),
            ]])
        if command == MILESTONE_ISSUES_COMMAND:
            return json.dumps([[{"number": 101, "title": "leftover one"},
                                {"number": 102, "title": "leftover two"}]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="Milestone #5") as excinfo:
        release.close_release_milestone("o/r", "v0.3.0")
    # The error carries the version, the Milestone number/url and the
    # open issue list — never a silent skip. The list was re-read with
    # the bounded backoff first (Issue #754) and stayed non-empty.
    assert "v0.3.0" in str(excinfo.value)
    assert "https://github.com/o/r/milestone/5" in str(excinfo.value)
    assert "#101" in str(excinfo.value)
    assert "#102" in str(excinfo.value)
    assert sleeps == [1.0, 2.0, 4.0]
    # No close was attempted.
    assert not [c for c in calls if "PATCH" in c]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_retries_the_stale_issue_index_after_close(monkeypatch):
    """Issue #754: `gh issue close` 返回成功不代表
    `issues?milestone=N&state=open` 索引已跟上（最终一致）——v0.4.10 在
    关票后 0-1 秒读到陈旧的非空列表，里程碑没有被关闭，release 评论
    留下 `milestone evidence unavailable`。读到非空时必须先按有界退避
    重查，索引跟上后正常关闭。"""
    calls = []
    sleeps: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.3.0", "open", 1)]])
        if command == MILESTONE_ISSUES_COMMAND:
            # First read (the same second as the close): stale index still
            # lists the just-closed issue; the re-read catches up.
            stale = json.dumps([[{"number": 748, "title": "Release v0.3.0"}]])
            return stale if calls.count(command) == 1 else json.dumps([[]])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps(_milestone(5, "v0.3.0", "closed", 0))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)
    evidence = release.close_release_milestone("o/r", "v0.3.0")
    assert "Milestone #5" in evidence
    assert "closed after release v0.3.0" in evidence
    # The stale read cost exactly one bounded backoff step, not a raise.
    assert sleeps == [1.0]
    assert calls[-1] == ["gh", "api", "repos/o/r/milestones/5",
                         "--method", "PATCH", "-f", "state=closed"]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_after_the_backoff_is_exhausted(monkeypatch):
    """Issue #754: 重试耗尽仍读到非空 → 仍然抛 RuntimeError——退避只是
    等索引，不是放松门禁；真有未完成的工作时照样 fail fast（带同样的
    version / milestone url / open issue 列表），且绝不 PATCH 关闭。"""
    calls = []
    sleeps: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.3.0", "open", 1)]])
        if command == MILESTONE_ISSUES_COMMAND:
            return json.dumps([[{"number": 101, "title": "leftover one"}]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="Milestone #5") as excinfo:
        release.close_release_milestone("o/r", "v0.3.0")
    assert "v0.3.0" in str(excinfo.value)
    assert "https://github.com/o/r/milestone/5" in str(excinfo.value)
    assert "#101" in str(excinfo.value)
    # Bounded: the delays are exhausted, then the gate fires.
    assert sleeps == [1.0, 2.0, 4.0]
    assert not [c for c in calls if "PATCH" in c]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_ignores_the_release_ticket_itself(monkeypatch):
    """Issue #808: 查到的 open issues 只有 release 票自己时视为已满足——
    票正被本次发版关闭，未刷新的 issue 索引把它算成未完成工作是秒级竞态
    （v0.2.0 因此没关 milestone）。确定性排除后不睡退避、不等索引刷新，
    直接关闭；成功证据注明排除，让用户看得见这次裁决。"""
    calls = []
    sleeps: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.3.0", "open", 1)]])
        if command == MILESTONE_ISSUES_COMMAND:
            # The release ticket was closed seconds ago; the stale index
            # still lists it — and nothing else.
            return json.dumps([[{"number": 99, "title": "Release v0.3.0"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps(_milestone(5, "v0.3.0", "closed", 0))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)
    evidence = release.close_release_milestone(
        "o/r", "v0.3.0", release_issue=99,
    )
    assert "closed after release v0.3.0" in evidence
    assert "release ticket #99 excluded" in evidence
    # Deterministic: no backoff waited for the index to refresh — the
    # race class is eliminated, not papered over with time.
    assert sleeps == []
    assert calls[-1] == ["gh", "api", "repos/o/r/milestones/5",
                         "--method", "PATCH", "-f", "state=closed"]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_refuses_real_leftovers_despite_ticket_exclusion(monkeypatch):
    """Issue #808 验收 2：守卫语义不放宽——release 票自身被排除后仍有
    真实 open issue 时照样拒绝关闭；错误列出真实遗留（release 票不出现在
    未完成列表里），并注明本次排除。"""
    calls = []
    sleeps: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.3.0", "open", 2)]])
        if command == MILESTONE_ISSUES_COMMAND:
            return json.dumps([[{"number": 99, "title": "Release v0.3.0"},
                                {"number": 101, "title": "leftover one"}]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr("time.sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="Milestone #5") as excinfo:
        release.close_release_milestone("o/r", "v0.3.0", release_issue=99)
    message = str(excinfo.value)
    assert "v0.3.0" in message
    assert "https://github.com/o/r/milestone/5" in message
    # The real leftover is named; the release ticket is annotated as
    # excluded, never listed as unfinished work.
    assert "#101 leftover one" in message
    assert "ticket #99 is excluded" in message
    assert "#99 Release v0.3.0" not in message
    # The #754 bounded backoff still ran for the non-empty leftovers,
    # then the gate refused. No close was attempted.
    assert sleeps == [1.0, 2.0, 4.0]
    assert not [c for c in calls if "PATCH" in c]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_on_duplicate_titles(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[
                _milestone(5, "v0.3.0", "open", 0),
                _milestone(7, "v0.3.0", "open", 0),
            ]])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="ambiguous") as excinfo:
        release.close_release_milestone("o/r", "v0.3.0")
    # Both candidates are named; neither is closed.
    assert "#5" in str(excinfo.value)
    assert "#7" in str(excinfo.value)
    assert calls == [MILESTONE_LIST_COMMAND]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_reconcile_release_epics_closes_verified_epic_with_audit(monkeypatch):
    commands = []
    epic = {"number": 20, "labels": [{"name": "ai-epic"}],
            "body": "## Children\n- https://github.com/o/r/issues/21",
            "blockedBy": {"nodes": []}}

    def fake_run(command, **kwargs):
        commands.append(command)
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=4&state=open&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[epic]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/20/sub_issues?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"number": 21, "repository": {"full_name": "o/r"}, "state": "closed"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/21"]:
            return json.dumps({"number": 21, "state": "closed"})
        if command == ["gh", "issue", "view", "20", "--repo", "o/r", "--json", "comments"]:
            return json.dumps({"comments": []})
        if command == ["gh", "issue", "close", "20", "--repo", "o/r"]:
            return ""
        raise AssertionError(command)

    # Match the exact formatted comment without coupling this test to its
    # wrapper's implementation.
    def fake_comment(number, *, repo, body):
        assert number == 20 and repo == "o/r"
        assert "Issue #21 closed" in body
        assert "run_id=abc12345" in body
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", fake_comment)
    result = release.reconcile_release_epics(
        "o/r", 4, "v0.4.0", "abc12345", "en",
    )
    assert result == ["Epic #20 closed after verification (Issue #21 closed)"]
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_release_epics_posts_the_audit_in_the_bound_language(monkeypatch):
    commands = []
    epic = {"number": 20, "labels": [{"name": "ai-epic"}],
            "body": "## Children\n- #21", "blockedBy": {"nodes": []}}

    def fake_run(command, **kwargs):
        commands.append(command)
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=4&state=open&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[epic]])
        if "sub_issues" in command[2]:
            return json.dumps([[{"number": 21, "repository": {"full_name": "o/r"}, "state": "closed"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/21"]:
            return json.dumps({"number": 21, "state": "closed"})
        if command == ["gh", "issue", "view", "20", "--repo", "o/r", "--json", "comments"]:
            return json.dumps({"comments": []})
        if command == ["gh", "issue", "close", "20", "--repo", "o/r"]:
            return ""
        raise AssertionError(command)

    bodies = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: bodies.append(body))
    release.reconcile_release_epics("o/r", 4, "v0.4.0", "abc12345", "zh")
    assert len(bodies) == 1
    assert "Epic 对账（v0.4.0）" in bodies[0]
    assert "Issue #21 closed" in bodies[0]
    assert "run_id=abc12345" in bodies[0]
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands
    with pytest.raises(AssertionError):
        fake_run(["gh", "api", "unexpected"])


def test_reconcile_release_epics_keeps_open_child_and_bad_scope_open(monkeypatch):
    calls = []
    issues = [
        {"number": 20, "labels": [{"name": "ai-epic"}],
         "body": "## Children\n- #21", "blockedBy": {"nodes": []}},
        {"number": 22, "labels": [{"name": "ai-epic"}],
         "body": "## Children", "blockedBy": {"nodes": []}},
    ]
    def fake_run(command, **kwargs):
        calls.append(command)
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=4&state=open&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([issues])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/20/sub_issues?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"number": 21, "repository": {"full_name": "o/r"}, "state": "open"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/21"]:
            return json.dumps({"number": 21, "state": "open"})
        if without_include(command) == ["gh", "api", "repos/o/r/issues/22/sub_issues?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[]])
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", lambda **kwargs: calls.append(["comment"]))
    result = release.reconcile_release_epics(
        "o/r", 4, "v0.4.0", "abc12345", "en",
    )
    assert "Epic #20 kept open: child Issue #21 is not closed" in result
    assert "Epic #22 kept open: Epic child scope is missing or empty" in result
    assert not any(command[:3] == ["gh", "issue", "close"] for command in calls if isinstance(command, list))
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_release_milestones_classifies_auth_and_not_found(monkeypatch):
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    errors = [
        ("gh: Bad credentials (HTTP 401)", 401),
        ("gh: Not Found (HTTP 404)", 404),
    ]
    for stderr, status in errors:
        def fake_run(command, **kwargs):
            if "milestones?state=all" in command[2]:
                return "[[]]"
            raise subprocess.CalledProcessError(1, command, stderr=stderr)

        monkeypatch.setattr(seam, "run_command", fake_run)
        with pytest.raises(github.MilestoneReconcileError) as caught:
            milestone.reconcile_release_milestones("o/r", "abc12345")
        assert caught.value.status == status
        assert caught.value.operation == "list_releases"
        assert caught.value.stderr == stderr


@pytest.mark.parametrize(
    ("failed_endpoint", "stderr", "operation"),
    [
        ("issues?milestone=5", "Bad credentials (HTTP 401)", "list_milestone_issues"),
        ("milestones/5", "Not Found (HTTP 404)", "close_milestone"),
    ],
)
def test_reconcile_release_milestones_classifies_later_operations(
    monkeypatch, failed_endpoint, stderr, operation,
):
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)

    def fake_run(command, **kwargs):
        endpoint = command[2]
        if failed_endpoint in endpoint:
            raise subprocess.CalledProcessError(1, command, stderr=stderr)
        if "milestones?state=all" in endpoint:
            return json.dumps([[_milestone(5, "v0.4.0", "open", 0)]])
        if "releases?per_page=100" in endpoint:
            return json.dumps([[{"tag_name": "v0.4.0", "draft": False}]])
        if "issues?milestone=5" in endpoint:
            return "[[]]"

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(github.MilestoneReconcileError) as caught:
        milestone.reconcile_release_milestones("o/r", "abc12345")
    assert caught.value.operation == operation
    assert caught.value.stderr == stderr
    assert fake_run(["gh", "api", "unexpected"]) is None


def test_reconcile_release_milestones_closes_only_published_empty_milestones(monkeypatch, caplog):
    caplog.set_level("INFO")
    calls = []
    call_kwargs = []
    listed_milestone = _milestone(5, "v0.4.0", "open", 0)
    def fake_run(command, **kwargs):
        calls.append(command)
        call_kwargs.append(kwargs)
        if without_include(command) == ["gh", "api", "repos/o/r/milestones?state=all&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[listed_milestone]])
        if without_include(command) == ["gh", "api", "repos/o/r/releases?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"tag_name": "v0.4.0", "draft": False}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[]])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones/5", "--method", "PATCH", "-f", "state=closed"]:
            return ""
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    assert milestone.reconcile_release_milestones("o/r", "abc12345") == ["Milestone #5 (v0.4.0) closed"]
    assert "milestone_closed number=5 repo=o/r" in caplog.text
    assert calls.index(["gh", "api", "repos/o/r/milestones/5", "--method", "PATCH", "-f", "state=closed"]) > calls.index(["gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100", "--paginate", "--slurp", "--include"])
    assert call_kwargs == [
        {"timeout": 30, "failure_log_level": logging.DEBUG},
        {"timeout": 30, "failure_log_level": logging.DEBUG},
        {"timeout": 30},
        {"timeout": 30},
    ]
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_release_milestones_keeps_open_without_published_release_or_empty_scope(monkeypatch, caplog):
    caplog.set_level("INFO")
    milestones = [_milestone(5, "v0.4.0", "open", 1), _milestone(6, "v0.4.0", "open", 0)]
    def fake_run(command, **kwargs):
        if without_include(command) == ["gh", "api", "repos/o/r/milestones?state=all&per_page=100", "--paginate", "--slurp"]:
            return json.dumps([milestones])
        if without_include(command) == ["gh", "api", "repos/o/r/releases?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"tag_name": "v0.4.0", "draft": True}]])
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    assert milestone.reconcile_release_milestones("o/r", "abc12345") == []
    assert 'reason="ambiguous title"' in caplog.text
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_release_milestones_keeps_malformed_or_incomplete_open(monkeypatch, caplog):
    caplog.set_level("INFO")
    milestones = [{"number": "bad", "title": "v0.4.0", "state": "open"}, {"number": 7, "title": "v0.7.0", "state": "invalid"}, _milestone(5, "v0.5.0", "open", 1), _milestone(6, "v0.6.0", "open", 0)]
    def fake_run(command, **kwargs):
        if "milestones?state=all" in command[2]:
            return json.dumps([milestones])
        if "releases?per_page=100" in command[2]:
            return json.dumps([[{"tag_name": "v0.5.0", "draft": False}]])
        if "issues?milestone=5" in command[2]:
            return json.dumps([[{"number": 9}]])
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    assert milestone.reconcile_release_milestones("o/r", "abc12345") == []
    assert "reason=malformed" in caplog.text
    assert 'reason="open issues"' in caplog.text
    assert 'reason="no published release"' in caplog.text
    with pytest.raises(AssertionError):
        fake_run(["x", "y", "z"])


def test_close_release_milestone_reconciles_epics_when_summary_lags(monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.4.0", "open", 1)]])
        if command == MILESTONE_ISSUES_COMMAND:
            return json.dumps([[{"number": 20}]]) if calls.count(command) == 1 else json.dumps([[]])
        if command[-1:] == ["state=closed"]:
            return ""
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(release, "reconcile_release_epics", lambda *args: ["Epic #20 closed"])
    result = release.close_release_milestone("o/r", "v0.4.0", run_id="abc12345")
    assert "Epic #20 closed" in result
    assert any(command[-1:] == ["state=closed"] for command in calls)
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_close_release_milestone_binds_the_language_to_the_epic_audit(monkeypatch):
    """Issue #1705: the release success path passes the run's bound
    language down to the Epic audit comment."""
    epic = {"number": 20, "labels": [{"name": "ai-epic"}],
            "body": "## Children\n- #21", "blockedBy": {"nodes": []}}
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([[_milestone(5, "v0.4.0", "open", 1)]])
        if command == MILESTONE_ISSUES_COMMAND:
            # The gate reads, the Epic sweep verifies, the gate re-reads
            # the Milestone after the sweep closed the Epic.
            return json.dumps([[epic]]) if calls.count(
                MILESTONE_ISSUES_COMMAND) < 3 else json.dumps([[]])
        if "sub_issues" in command[2]:
            return json.dumps([[{"number": 21,
                                 "repository": {"full_name": "o/r"},
                                 "state": "closed"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/21"]:
            return json.dumps({"number": 21, "state": "closed"})
        if command == ["gh", "issue", "view", "20", "--repo", "o/r",
                       "--json", "comments"]:
            return json.dumps({"comments": []})
        if command == ["gh", "issue", "close", "20", "--repo", "o/r"]:
            return ""
        if command[-1:] == ["state=closed"]:
            return ""
        raise AssertionError(command)

    bodies = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: bodies.append(body))
    result = release.close_release_milestone(
        "o/r", "v0.4.0", run_id="abc12345", language_code="zh",
    )
    assert "Epic #20 closed" in result
    assert len(bodies) == 1
    assert "Epic 对账（v0.4.0）" in bodies[0]
    assert "Issue #21 closed" in bodies[0]
    with pytest.raises(AssertionError):
        fake_run(["gh", "api", "unexpected"])


def test_reconcile_release_epics_keeps_blocked_and_avoids_duplicate_audit(monkeypatch):
    calls = []
    issues = [
        {"number": 30, "labels": [{"name": "ai-epic"}],
         "body": "## Children\n- #31"},
        {"number": 32, "labels": [{"name": "ai-epic"}],
         "body": "## Children\n- #33", "blockedBy": {"nodes": [{"number": 9, "state": "OPEN"}]}},
        {"number": 34, "labels": [{"name": "ai-epic"}],
         "body": "## Children\n- #35", "blockedBy": {"nodes": []}},
    ]
    audit = "<!-- orbi:run=abc12345 -->\nEpic reconciliation for v0.4.0: complete; children: Issue #35 closed; no open native blockers/dependencies."
    def fake_run(command, **kwargs):
        calls.append(command)
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=4&state=open&per_page=100",
                       "--paginate", "--slurp"]:
            return json.dumps([issues])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/34/sub_issues?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"number": 35, "repository": {"full_name": "o/r"}, "state": "closed"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/35"]:
            return json.dumps({"number": 35, "state": "closed"})
        if command == ["gh", "issue", "view", "30", "--repo", "o/r",
                       "--json", "number,body,labels,blockedBy"]:
            return json.dumps({"number": 30, "body": "## Children\n- #31",
                               "labels": [{"name": "ai-epic"}],
                               "blockedBy": {"nodes": []}})
        if command == ["gh", "issue", "view", "30", "--repo", "o/r", "--json", "comments"]:
            return json.dumps({"comments": []})
        if without_include(command) == ["gh", "api", "repos/o/r/issues/30/sub_issues?per_page=100", "--paginate", "--slurp"]:
            return json.dumps([[{"number": 31, "repository": {"full_name": "o/r"}, "state": "closed"}]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues/31"]:
            return json.dumps({"number": 31, "state": "closed"})
        if command[:4] == ["gh", "issue", "view", "34"]:
            return json.dumps({"comments": [{"body": audit}]})
        if tuple(command) in {
            ("gh", "issue", "close", "30", "--repo", "o/r"),
            ("gh", "issue", "close", "34", "--repo", "o/r"),
        }:
            return ""
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: calls.append(["comment"]))
    result = release.reconcile_release_epics(
        "o/r", 4, "v0.4.0", "abc12345", "zh",
    )
    assert any("Epic #30 closed after verification" in item for item in result)
    assert any("open blockers: #9" in item for item in result)
    assert ["gh", "issue", "close", "34", "--repo", "o/r"] in calls
    assert ["gh", "issue", "close", "30", "--repo", "o/r"] in calls
    assert ["gh", "issue", "close", "32", "--repo", "o/r"] not in calls
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_git_out_helper_fails_fast_on_nonzero_exit(tmp_path):
    """The helper must fail fast (no silent swallow) when git exits
    non-zero — the same contract as the other git smoke helpers."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git_out(tmp_path, "rev-parse", "no-such-ref")


def test_tag_commit_is_ancestor_of_base_true_when_ancestor(tmp_path):
    """The tag commit is an ancestor of the advanced base (and not vice
    versa) — the resume case the release state machine recovers."""
    work = make_release_range_repo(tmp_path)
    tag_commit = commit_file(work, "version.txt", "chore: v0.4.0")
    base = commit_file(work, "later.txt", "later")
    assert release.tag_commit_is_ancestor_of_base(
        tag_commit, base, work) is True
    assert release.tag_commit_is_ancestor_of_base(
        base, tag_commit, work) is False


def test_tag_commit_is_ancestor_of_base_false_on_unrelated_commits(
        tmp_path):
    """A tag commit on an unrelated side branch is NOT an ancestor of the
    base — the genuine tag mismatch that must fail fast."""
    work = make_release_range_repo(tmp_path)
    base = commit_file(work, "base.txt", "base")
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "side"],
                   check=True, capture_output=True)
    commit_file(work, "side.txt", "side")
    side_head = git_out(work, "rev-parse", "HEAD")
    assert release.tag_commit_is_ancestor_of_base(
        side_head, base, work) is False


def test_process_release_recovers_the_tag_when_the_base_advanced(
        monkeypatch):
    """A previous attempt pushed the tag and the base advanced past it
    afterwards (for example a merge landed while the release was in
    flight). On a resume the frozen base is that later commit; the release
    must recover the tag commit as the canonical release commit instead of
    deadlocking on the tag check."""
    state = make_release_process_env(monkeypatch, tag_commit="abc123")
    # The frozen base is a descendant of the tag commit; the env's
    # fake_run_command answers "ancestor" for the merge-base check.
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "later456")
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    release_url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert release_url == "https://github.com/o/r/releases/tag/v0.3.0"
    # The tag commit was recovered as the canonical release commit.
    assert state["published"][0]["release_commit"] == "abc123"
    # The release succeeded (ai-merged), not blocked.
    assert any(k.get("add") == "ai-merged" for _, k in state["edits"])


def test_close_release_milestone_rejects_a_non_array_milestone_list(monkeypatch):
    def fake_run(command, **kwargs):
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps({"number": 5, "title": "v0.3.0"})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(ValueError, match="must be an array of arrays"):
        release.close_release_milestone("o/r", "v0.3.0")
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_reraises_a_real_gh_failure(monkeypatch):
    def fake_run(command, **kwargs):
        if command == MILESTONE_LIST_COMMAND:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: rate limit exceeded",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        release.close_release_milestone("o/r", "v0.3.0")
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_parse_release_declaration_rejects_non_string_body():
    with pytest.raises(ValueError, match="must be a string"):
        release.parse_release_declaration(123)


def test_parse_release_declaration_rejects_empty_section():
    # `## Release` is the very last line: the section holds no fields.
    with pytest.raises(ValueError, match="version"):
        release.parse_release_declaration("Ship it.\n\n## Release\n")


def test_parse_release_declaration_rejects_colonless_field_line():
    # A `- ` line without a colon BEFORE the scope list is a malformed
    # field line (after the scope list it is a malformed scope item).
    body = RELEASE_DECLARATION_BODY.replace(
        "## Release\n", "## Release\n\n- broken\n",
    )
    with pytest.raises(ValueError, match=r"field 'broken' is malformed"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_inline_scope_value():
    body = RELEASE_DECLARATION_BODY.replace("- scope:\n", "- scope: 123\n")
    with pytest.raises(ValueError, match="not an inline value"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_plain_line_in_scope():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "hello")
    with pytest.raises(ValueError, match=r"scope item 'hello' is malformed"):
        release.parse_release_declaration(body)


def test_parse_release_declaration_rejects_plain_line_outside_scope():
    # A plain line while the scope list is NOT open is rejected as a
    # non-field line (the same line inside the scope list is a
    # malformed scope item).
    body = RELEASE_DECLARATION_BODY.replace(
        "## Release\n", "## Release\n\nhello\n",
    )
    with pytest.raises(ValueError, match=r"line 'hello' is not a"):
        release.parse_release_declaration(body)


def test_verify_release_scope_reraises_real_issue_gh_failure(monkeypatch):
    def fail(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"]:
            raise subprocess.CalledProcessError(
                1, command,
                stderr="GraphQL: Could not resolve to a PullRequest with "
                       "the number of 7.",
            )
        if command[:3] == ["gh", "issue", "view"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: rate limited",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fail)
    monkeypatch.setattr(seam, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        release.verify_release_scope("o/r", [7], Path("/repo"), "release123")
    assert "HTTP 403: rate limited" in str(excinfo.value.stderr)
    # The fake rejects anything that is not pr/issue view traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fail(["gh", "release", "list"])


def test_process_release_keeps_the_fresh_id_when_no_run_id_is_recoverable(
        monkeypatch):
    # ai-in-progress is present but no run id can be recovered: the
    # fresh id is kept (the resume branch is skipped).
    state = make_release_process_env(monkeypatch, in_progress=True,
                                     existing_run_id=None)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"},
                        {"name": "ai-in-progress"}]}
    release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert state["run_ids"] == ["a1b2c3d4"]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]


def test_process_release_publishes_the_release_role_progress_body(
        monkeypatch):
    # `_safe_publish` really invokes the action here, so the progress
    # closure renders the body: role=release, run marker, branch.
    state = make_release_process_env(monkeypatch)
    publishes = []

    def publish(**kwargs):
        publishes.append(kwargs)
        kwargs["action"]()

    monkeypatch.setattr(seam, "_safe_publish", publish)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert publishes, "no progress publish happened"
    assert all(kwargs["role"] == "release" for kwargs in publishes)
    assert all(kwargs["issue"] == 99 for kwargs in publishes)
    publisher = release.ProgressPublisher.return_value
    assert publisher.ensure.call_count == 1
    body = publisher.ensure.call_args[0][0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "- role: release" in body
    assert "- priority: normal" in body
    assert "- branch: orbi/o-r-issue-99" in body


def test_process_release_fails_on_scope_item_that_is_neither(monkeypatch):
    # Scope item #125 is neither a PR nor an Issue on the remote: the
    # item-by-item verification fails fast and the release is blocked.
    body = RELEASE_DECLARATION_BODY.replace("  - #124", "  - #125")
    state = make_release_process_env(monkeypatch, body=body)
    issue = {"number": 99, "title": "Release v0.3.0", "body": body,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    result = release.process_release(
        issue, config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"), "o/r",
    )
    assert result == ""
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                        "remove": "ai-in-progress"})


def test_scope_gh_fake_rejects_unexpected_command(monkeypatch):
    make_scope_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "release", "list"])


def test_gate_gh_fake_rejects_unexpected_command(monkeypatch):
    make_gate_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "release", "list"])


def test_release_gh_fake_rejects_unexpected_command(monkeypatch):
    make_release_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["git", "tag", "v0.3.0"])


def test_release_process_env_fake_rejects_unexpected_command(monkeypatch):
    make_release_process_env(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "label", "list"])


def test_release_process_env_fake_answers_the_real_tag_probe(tmp_path,
                                                             monkeypatch):
    # The env fake answers the `git ls-remote` of the REAL
    # `release_tag_commit` (the lock is taken for real on tmp_path).
    real_release_tag_commit = release.release_tag_commit
    make_release_process_env(monkeypatch)
    monkeypatch.setattr(release, "release_tag_commit",
                        real_release_tag_commit)

    def fake(command, **kwargs):
        if command[:2] == ["git", "ls-remote"]:
            return ("abc123\trefs/tags/v0.3.0\n"
                    "abc123\trefs/tags/v0.3.0^{}\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(seam, "run_command", fake)
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["git", "fetch", "origin"])
    assert release.release_tag_commit(tmp_path, "v0.3.0") == "abc123"
