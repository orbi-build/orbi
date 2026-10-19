"""Behavioral tests for the scoped orbi-status observation (Issue #1656).

The settled contract lives in docs/public-contracts.mdx; these tests
exercise the observable behavior with synthetic repositories, JSONL
events, GitHub responses and controlled time: scope/selection, exact
attribution, active-set membership, confirmed removal, the two read
cycles, bounded termination and the failure path.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import orbi.status_observe as observe
from orbi import cli
from orbi.config import RunnerConfig
from seam import seam


def _issue(number, *, title="task", url="u", state="open",
           labels=("ai-in-progress",), pull_request=None):
    issue = {
        "number": number, "title": title, "url": url, "state": state,
        "labels": [{"name": name} for name in labels],
    }
    if pull_request is not None:
        issue["pull_request"] = pull_request
    return issue


def _config(tmp_path, repos=("acme/shop",), pi_providers=None):
    return RunnerConfig(
        source_repos=tuple(repos), repo_dir=tmp_path, base_branch="main",
        max_concurrency=1, slot_dir=tmp_path / ".orbi" / "slots",
        pi_providers=pi_providers,
    )


def _worktree(root, name):
    path = root / ".worktrees" / name
    path.mkdir(parents=True)
    return path


def _assistant(items):
    return {"type": "message",
            "message": {"role": "assistant", "content": items}}


# --- selector validation (before any read) ---------------------------------


def test_parse_scope_defaults_to_all_configured_repositories():
    scope = observe.parse_scope(("acme/shop", "acme/blog"))
    assert scope.repos == ("acme/shop", "acme/blog")
    assert scope.issue is None


def test_parse_scope_repo_selects_one_repository():
    scope = observe.parse_scope(("acme/shop", "acme/blog"), repo="acme/blog")
    assert scope.repos == ("acme/blog",)


def test_parse_scope_rejects_unknown_repository():
    with pytest.raises(observe.StatusSelectorError, match="unknown repository"):
        observe.parse_scope(("acme/shop",), repo="other/repo")


def test_parse_scope_accepts_issue_with_explicit_repo():
    scope = observe.parse_scope(
        ("acme/shop", "acme/blog"), repo="acme/shop", issue=42,
    )
    assert scope.repos == ("acme/shop",)
    assert scope.issue == 42


def test_parse_scope_accepts_issue_with_one_repository():
    assert observe.parse_scope(("acme/shop",), issue=42).issue == 42


def test_parse_scope_rejects_issue_without_repo_when_several_configured():
    with pytest.raises(observe.StatusSelectorError, match="requires --repo"):
        observe.parse_scope(("acme/shop", "acme/blog"), issue=42)


@pytest.mark.parametrize("issue", [0, -1, True, "x"])
def test_parse_scope_rejects_non_positive_issue(issue):
    with pytest.raises(observe.StatusSelectorError, match="invalid issue number"):
        observe.parse_scope(("acme/shop",), issue=issue)


# --- GitHub delivery mapping -----------------------------------------------


def test_delivery_from_issue_maps_fields():
    delivery = observe.delivery_from_issue(
        "acme/shop",
        _issue(3, title="T", url="U", labels=("ai-in-progress", "bug")),
    )
    assert delivery == observe.Delivery(
        "acme/shop", 3, "T", "U", "open", ("ai-in-progress", "bug"),
    )


def test_delivery_from_issue_accepts_rest_html_url_and_uppercase_state():
    delivery = observe.delivery_from_issue("acme/shop", {
        "number": 3, "title": "T", "html_url": "H", "state": "CLOSED",
        "labels": [{"name": "ai-merged"}, {"not_name": 1}, "raw"],
    })
    assert delivery.url == "H"
    assert delivery.state == "closed"
    assert delivery.labels == ("ai-merged",)


def test_delivery_from_issue_defaults_missing_optional_fields():
    delivery = observe.delivery_from_issue("acme/shop", {"number": 3})
    assert delivery.title == ""
    assert delivery.url == ""
    assert delivery.state == ""
    assert delivery.labels == ()


def test_delivery_from_issue_rejects_non_object():
    with pytest.raises(ValueError, match="not an object"):
        observe.delivery_from_issue("acme/shop", "nope")


def test_delivery_from_issue_rejects_missing_number():
    with pytest.raises(ValueError, match="no numeric number"):
        observe.delivery_from_issue("acme/shop", {"title": "T"})


def test_delivery_from_issue_rejects_boolean_number():
    with pytest.raises(ValueError, match="no numeric number"):
        observe.delivery_from_issue("acme/shop", {"number": True})


def test_is_active_matches_only_active_delivery_labels():
    assert observe.is_active(
        observe.delivery_from_issue("acme/shop", _issue(1)),
    ) is True
    assert observe.is_active(
        observe.delivery_from_issue("acme/shop", _issue(2, labels=("ai-merged",))),
    ) is False


def test_open_issues_complete_reads_paginated_pages(monkeypatch):
    calls = []

    def fake_run(command, timeout=None):
        calls.append((command, timeout))
        return '[[{"number": 1}]]'

    monkeypatch.setattr(observe, "run_gh_read_command", fake_run)
    assert observe.open_issues_complete("acme/shop") == [{"number": 1}]
    assert calls == [(
        ["gh", "api", "repos/acme/shop/issues?state=open&per_page=100",
         "--paginate", "--slurp"], 30,
    )]


def test_read_active_deliveries_excludes_prs_closed_and_inactive():
    raw = [
        _issue(1),
        _issue(2, pull_request={}),
        _issue(3, state="closed"),
        _issue(4, labels=("ai-merged",)),
        _issue(5, labels=("ai-fix-needed",)),
    ]
    deliveries = observe.read_active_deliveries(
        "acme/shop", read_issues=lambda repo: raw,
    )
    assert [delivery.number for delivery in deliveries] == [1, 5]


def test_read_active_deliveries_sorts_by_number():
    raw = [_issue(9), _issue(2)]
    deliveries = observe.read_active_deliveries(
        "acme/shop", read_issues=lambda repo: raw,
    )
    assert [delivery.number for delivery in deliveries] == [2, 9]


def test_confirmed_delivery_reads_one_issue():
    calls = []

    def read_issue(number, fields, *, repo, timeout=None):
        calls.append((number, fields, repo))
        return _issue(number, state="closed", labels=("ai-merged",))

    delivery = observe.confirmed_delivery(
        "acme/shop", 7, read_issue=read_issue,
    )
    assert delivery.state == "closed"
    assert calls == [(7, observe.ISSUE_FIELDS, "acme/shop")]


# --- worktree and session attribution --------------------------------------


def test_worktree_attributions_returns_empty_without_worktrees_dir(tmp_path):
    assert observe.worktree_attributions(tmp_path, "acme/shop") == []


def test_worktree_attributions_uses_the_full_naming_convention(tmp_path):
    good = _worktree(tmp_path, "orbi-acme-shop-issue-42-86b57234")
    _worktree(tmp_path, "orbi-acme-shop-issue-420-86b57234")
    _worktree(tmp_path, "orbi-acme-shop-issue-42-nothex")
    _worktree(tmp_path, "orbi-acme-blog-issue-42-86b57234")
    (tmp_path / ".worktrees" / "plain-file").write_text("x", encoding="utf-8")
    trees = observe.worktree_attributions(tmp_path, "acme/shop", 42)
    assert [(tree.issue, tree.run_id) for tree in trees] == [(42, "86b57234")]
    assert trees[0].path == good


def test_worktree_attributions_skips_malformed_names(tmp_path):
    base = tmp_path / ".worktrees"
    base.mkdir()
    for name in (
        "orbi-acme-shop-issue-",
        "orbi-acme-shop-issue-x-86b57234",
        "unrelated",
    ):
        (base / name).mkdir()
    assert observe.worktree_attributions(tmp_path, "acme/shop") == []


def test_worktree_attributions_sorts_by_issue_and_run(tmp_path):
    _worktree(tmp_path, "orbi-acme-shop-issue-7-bbbbbbbb")
    _worktree(tmp_path, "orbi-acme-shop-issue-7-aaaaaaaa")
    _worktree(tmp_path, "orbi-acme-shop-issue-3-cccccccc")
    trees = observe.worktree_attributions(tmp_path, "acme/shop")
    assert [(tree.issue, tree.run_id) for tree in trees] == [
        (3, "cccccccc"), (7, "aaaaaaaa"), (7, "bbbbbbbb"),
    ]


def test_session_files_returns_empty_without_session_dir(tmp_path):
    assert observe.session_files(tmp_path) == []


def test_session_files_sorted_by_name_and_files_only(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    (session_dir / "b.jsonl").write_text("", encoding="utf-8")
    (session_dir / "a.jsonl").write_text("", encoding="utf-8")
    (session_dir / "c.jsonl").mkdir()
    (session_dir / "notes.txt").write_text("", encoding="utf-8")
    assert [path.name for path in observe.session_files(tmp_path)] == [
        "a.jsonl", "b.jsonl",
    ]


# --- path policy -----------------------------------------------------------


def test_safe_target_normalizes_a_relative_path(tmp_path):
    assert observe.safe_target(tmp_path, "./src/login.ts") == "src/login.ts"


def test_safe_target_rejects_absolute_and_escaping_paths(tmp_path):
    assert observe.safe_target(tmp_path, "/etc/passwd") is None
    assert observe.safe_target(tmp_path, "../secrets") is None
    assert observe.safe_target(tmp_path, "..") is None
    assert observe.safe_target(tmp_path, ".") is None
    assert observe.safe_target(tmp_path, "a/../../b") is None


def test_safe_target_publishes_an_absolute_in_worktree_path(tmp_path):
    """The shared path policy: an absolute target inside the worktree is
    published as its repository-relative form, never as an absolute one."""
    assert observe.safe_target(
        tmp_path, str(tmp_path / "src" / "login.ts"),
    ) == "src/login.ts"


def test_safe_target_rejects_non_string_and_empty(tmp_path):
    assert observe.safe_target(tmp_path, None) is None
    assert observe.safe_target(tmp_path, "  ") is None


def test_safe_target_rejects_runtime_and_git_paths(tmp_path):
    assert observe.safe_target(tmp_path, ".orbi/plan.md") is None
    assert observe.safe_target(tmp_path, ".worktrees/x") is None
    assert observe.safe_target(tmp_path, ".pi-session/x.jsonl") is None
    assert observe.safe_target(tmp_path, ".pi/x") is None
    assert observe.safe_target(tmp_path, "plan.md") is None
    assert observe.safe_target(tmp_path, "test.log") is None
    assert observe.safe_target(tmp_path, "verify.md") is None
    assert observe.safe_target(tmp_path, ".git/config") is None
    assert observe.safe_target(tmp_path, "src/.git/x") is None


def test_local_observer_excludes_the_configured_provider_file(tmp_path):
    """The credential/provider file the existing configuration already
    resolved is null even when it sits inside the worktree."""
    tree = _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    provider = tree / "provider.json"
    provider.write_text("{}", encoding="utf-8")
    session = tree / ".pi-session"
    session.mkdir()
    (session / "s.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-10-15T06:20:18Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "read",
                 "arguments": {"path": "provider.json"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, pi_providers=provider)
    observer = observe.StatusObserver(
        config, observe.parse_scope(config.source_repos),
    )
    entry = observer.observations("acme/shop", {1})[1][0]
    assert entry.kind == "read"
    assert entry.target is None


def test_safe_target_rejects_a_configured_excluded_path(tmp_path):
    excluded = str((tmp_path / "secrets" / "token").resolve())
    assert observe.safe_target(
        tmp_path, "secrets/token", exclude_paths=frozenset({excluded}),
    ) is None


# --- activity classification -----------------------------------------------


def test_activity_of_classifies_read_with_target():
    record = _assistant([
        {"type": "toolCall", "name": "read",
         "arguments": {"path": "src/a.ts"}},
    ])
    assert observe.activity_of(record) == ("read", "src/a.ts")


def test_activity_of_classifies_edit_with_file_path():
    record = _assistant([
        {"type": "toolCall", "name": "write",
         "arguments": {"file_path": "src/b.ts"}},
    ])
    assert observe.activity_of(record) == ("edit", "src/b.ts")


def test_activity_of_classifies_read_without_an_argument():
    record = _assistant([
        {"type": "toolCall", "name": "read", "arguments": "x"},
    ])
    assert observe.activity_of(record) == ("read", None)


def test_activity_of_read_tool_without_a_path_key():
    record = _assistant([
        {"type": "toolCall", "name": "read", "arguments": {"other": 1}},
    ])
    assert observe.activity_of(record) == ("read", None)


def test_activity_of_classifies_command_without_target():
    record = _assistant([
        {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}},
    ])
    assert observe.activity_of(record) == ("command", None)


def test_activity_of_skips_a_tool_outside_the_whitelist():
    """A tool the progress whitelist does not carry publishes no
    activity, exactly as the published activity does."""
    record = _assistant([{"type": "toolCall", "name": "webfetch"}])
    assert observe.activity_of(record) is None


def test_activity_of_takes_the_newest_content_item():
    """The real Pi assistant message carries thinking before its tool
    call, and the progress writer folds the newest item: the local
    observation reports the same kind and target for that record."""
    record = _assistant([
        {"type": "thinking", "thinking": "..."},
        {"type": "toolCall", "name": "codemode", "arguments": {"code": "x"}},
    ])
    assert observe.activity_of(record) == ("command", None)
    read_after_text = _assistant([
        {"type": "text", "text": "..."},
        {"type": "toolCall", "name": "edit",
         "arguments": {"file_path": "src/a.ts"}},
    ])
    assert observe.activity_of(read_after_text) == ("edit", "src/a.ts")


def test_activity_of_classifies_text_as_analysis():
    record = _assistant([{"type": "text", "text": "..."}])
    assert observe.activity_of(record) == ("analysis", None)


def test_activity_of_ignores_non_activity_records():
    assert observe.activity_of({"type": "session"}) is None
    assert observe.activity_of({"type": "message", "message": "x"}) is None
    assert observe.activity_of({
        "type": "message", "message": {"role": "user", "content": []},
    }) is None
    assert observe.activity_of({
        "type": "message", "message": {"role": "assistant", "content": "x"},
    }) is None
    assert observe.activity_of(_assistant(["raw", {"type": "other"}])) is None


# --- local observation -----------------------------------------------------


def test_local_observer_returns_nothing_without_matching_worktrees(tmp_path):
    assert observe.LocalObserver(tmp_path).observations("acme/shop", {1}) == {}


def test_local_observer_reports_no_readable_activity_without_a_session(tmp_path):
    _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    entries = observe.LocalObserver(tmp_path).observations("acme/shop", {1})
    assert entries[1][0].run_id == "aaaaaaaa"
    assert entries[1][0].session_id is None
    assert entries[1][0].at is None


def test_local_observer_attributes_session_and_activity(tmp_path):
    tree = _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    session = tree / ".pi-session"
    session.mkdir()
    (session / "s.jsonl").write_text(
        json.dumps({
            "type": "session", "id": "sess-1",
            "timestamp": "2026-10-15T06:20:00Z",
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-10-15T06:20:18Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "read",
                 "arguments": {"path": "src/login.ts"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    entry = observe.LocalObserver(tmp_path).observations("acme/shop", {1})[1][0]
    assert entry.session_id == "sess-1"
    assert entry.at == "2026-10-15T06:20:18Z"
    assert entry.kind == "read"
    assert entry.target == "src/login.ts"


def test_local_observer_reads_only_new_records_on_the_next_cycle(tmp_path):
    tree = _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    session = tree / ".pi-session"
    session.mkdir()
    path = session / "s.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "sess-1"}) + "\n",
        encoding="utf-8",
    )
    observer = observe.LocalObserver(tmp_path)
    assert observer.observations("acme/shop", {1})[1][0].session_id == "sess-1"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "message", "timestamp": "2026-10-15T06:20:18Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "bash",
                 "arguments": {"command": "ls"}},
            ]},
        }) + "\n")
    entry = observer.observations("acme/shop", {1})[1][0]
    assert entry.kind == "command"
    assert entry.target is None


def test_local_observer_ignores_incomplete_records(tmp_path):
    tree = _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    session = tree / ".pi-session"
    session.mkdir()
    (session / "s.jsonl").write_text(
        json.dumps({"type": "session"}) + "\n"
        + json.dumps({"type": "other"}) + "\n"
        + json.dumps({
            "type": "message", "timestamp": 123,
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "read",
                 "arguments": {"path": "src/a.ts"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    entry = observe.LocalObserver(tmp_path).observations("acme/shop", {1})[1][0]
    assert entry.session_id is None
    assert entry.kind == "read"
    assert entry.at is None


def test_local_observer_keeps_ambiguous_worktrees_separate(tmp_path):
    _worktree(tmp_path, "orbi-acme-shop-issue-1-aaaaaaaa")
    _worktree(tmp_path, "orbi-acme-shop-issue-1-bbbbbbbb")
    entries = observe.LocalObserver(tmp_path).observations("acme/shop", {1})
    assert [entry.run_id for entry in entries[1]] == ["aaaaaaaa", "bbbbbbbb"]


# --- rendering -------------------------------------------------------------


def test_observation_lines_without_observations():
    assert observe.observation_lines([]) == ["    observation: none"]


def test_observation_lines_render_no_readable_activity():
    entry = observe.Observation("aaaaaaaa", None, None, None, None, None)
    assert observe.observation_lines([entry]) == [
        "    observation: run=aaaaaaaa no readable activity",
    ]


def test_observation_lines_render_full_attribution():
    entry = observe.Observation(
        "aaaaaaaa", "sess-1", Path("/x"), "2026-10-15T06:20:18Z",
        "read", "src/a.ts",
    )
    assert observe.observation_lines([entry]) == [
        "    observation: run=aaaaaaaa session=sess-1 "
        "at=2026-10-15T06:20:18Z kind=read target=src/a.ts",
    ]


def test_observation_lines_render_null_target_as_dash():
    entry = observe.Observation(
        "aaaaaaaa", "sess-1", Path("/x"), "2026-10-15T06:20:18Z",
        "command", None,
    )
    assert observe.observation_lines([entry]) == [
        "    observation: run=aaaaaaaa session=sess-1 "
        "at=2026-10-15T06:20:18Z kind=command target=-",
    ]


def test_format_delivery_includes_state_and_labels():
    delivery = observe.Delivery(
        "acme/shop", 3, "T", "U", "open", ("ai-in-progress",),
    )
    assert observe.format_delivery(delivery) == (
        "#3 T U state=open labels=ai-in-progress"
    )


def test_format_delivery_omits_empty_state_labels_and_title():
    delivery = observe.Delivery("acme/shop", 3, "", "", "", ())
    assert observe.format_delivery(delivery) == "#3"


# --- observer and cycles ---------------------------------------------------


def test_observer_due_before_and_after_refresh(tmp_path):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: [], now=lambda: 100.0,
    )
    assert observer.due() is True
    observer.refresh()
    assert observer.due() is False
    assert observer.due(100.0 + observe.GITHUB_CYCLE_SECONDS) is True


def test_observer_refresh_enumerates_all_active_deliveries(tmp_path):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    raw = [_issue(5), _issue(2), _issue(9, labels=("ai-merged",))]
    observer = observe.StatusObserver(config, scope, read_issues=lambda repo: raw)
    observer.refresh()
    assert [delivery.number for delivery in observer.deliveries["acme/shop"]] == [
        2, 5,
    ]


def test_observer_refresh_reads_confirmed_state_for_an_issue_scope(tmp_path):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos, issue=7)
    observer = observe.StatusObserver(
        config, scope,
        read_issue=lambda number, fields, *, repo, timeout=None: _issue(
            number, state="closed", labels=("ai-merged",),
        ),
    )
    observer.refresh()
    assert observer.confirmed["acme/shop"].state == "closed"
    assert observer.deliveries == {}


def test_observer_reports_an_ended_delivery_with_its_confirmed_state(tmp_path):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    state = {"raw": [_issue(1), _issue(2)]}
    reads = []

    def read_issue(number, fields, *, repo, timeout=None):
        reads.append(number)
        return _issue(number, state="closed", labels=("ai-merged",))

    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: state["raw"],
        read_issue=read_issue,
    )
    observer.refresh()
    state["raw"] = [_issue(2), _issue(3)]
    observer.refresh()
    assert reads == [1]
    assert observer.ended == [
        "  ended: #1 task u state=closed labels=ai-merged",
    ]


def test_observer_reads_base_queue_and_result_on_the_github_cycle(tmp_path):
    """The base, the ready queue and the recent result are GitHub-backed
    view data of the same cycle as the active set (Issue #1656)."""
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    calls = []
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: [],
        read_base=lambda repo_dir, branch: calls.append(
            ("base", repo_dir, branch),
        ) or "abc123",
        read_ready=lambda repo: calls.append(("ready", repo)) or {"n": 11},
        read_result=lambda repo: calls.append(("result", repo)) or None,
    )
    observer.refresh()
    assert calls == [
        ("base", tmp_path, "main"), ("ready", "acme/shop"),
        ("result", "acme/shop"),
    ]
    assert observer.base["acme/shop"] == "abc123"
    assert observer.ready["acme/shop"] == {"n": 11}
    assert observer.result["acme/shop"] is None


def test_render_re_reads_no_github_state(tmp_path):
    """A local render (the 0.5-second cycle) re-reads no GitHub state:
    the queue/result/base come from the last complete cycle."""
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    reads = []
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: [],
        read_base=lambda repo_dir, branch: reads.append("base") or "abc123",
        read_ready=lambda repo: reads.append("ready") or None,
        read_result=lambda repo: reads.append("result") or None,
    )
    observer.refresh()
    reads.clear()
    report = cli.status_report(config, scope, observer)
    assert "  base: main abc123" in report
    assert reads == []


def test_render_keeps_each_issue_and_run_attributed(tmp_path):
    """Acceptance: an Issue that rotated implementation -> review run
    keeps its own attributed runs/sessions while another Issue produces
    newer files; neither picks up the other's activity."""
    def write_jsonl(tree_name, session_id, at, target):
        session = tmp_path / ".worktrees" / tree_name / ".pi-session"
        session.mkdir(parents=True)
        (session / "s.jsonl").write_text(
            json.dumps({
                "type": "session", "id": session_id,
                "timestamp": "2026-10-15T06:00:00Z",
            }) + "\n"
            + json.dumps({
                "type": "message", "timestamp": at,
                "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "name": "read",
                     "arguments": {"path": target}},
                ]},
            }) + "\n",
            encoding="utf-8",
        )

    write_jsonl(
        "orbi-acme-shop-issue-3-aaaaaaaa", "sess-old",
        "2026-10-15T06:10:00Z", "src/old.ts",
    )
    write_jsonl(
        "orbi-acme-shop-issue-3-bbbbbbbb", "sess-new",
        "2026-10-15T06:20:00Z", "src/login.ts",
    )
    write_jsonl(
        "orbi-acme-shop-issue-5-cccccccc", "sess-5",
        "2026-10-15T06:30:00Z", "src/other.ts",
    )
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: [_issue(3), _issue(5)],
        read_base=lambda repo_dir, branch: "abc123",
        read_ready=lambda repo: None, read_result=lambda repo: None,
    )
    observer.refresh()
    blocks = cli.status_report(config, scope, observer).split("  active: #")
    assert len(blocks) == 3
    assert "run=aaaaaaaa session=sess-old" in blocks[1]
    assert "run=bbbbbbbb session=sess-new" in blocks[1]
    assert "run=cccccccc session=sess-5" in blocks[2]
    assert "run=aaaaaaaa" not in blocks[2]
    assert "run=bbbbbbbb" not in blocks[2]


def test_observer_covers_two_repositories_and_discovers_new_tasks(tmp_path):
    config = _config(tmp_path, repos=("acme/shop", "acme/blog"))
    scope = observe.parse_scope(config.source_repos)
    state = {"acme/shop": [_issue(1)], "acme/blog": []}
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: state[repo],
    )
    observer.refresh()
    assert [delivery.number for delivery in observer.deliveries["acme/shop"]] == [1]
    assert observer.deliveries["acme/blog"] == []
    state["acme/blog"] = [_issue(9)]
    observer.refresh()
    assert [delivery.number for delivery in observer.deliveries["acme/blog"]] == [9]


def test_scope_label_names_repository_and_issue():
    scope = observe.parse_scope(("acme/shop", "acme/blog"))
    assert observe.scope_label(scope) == "acme/shop,acme/blog"
    scoped = observe.parse_scope(("acme/shop",), issue=5)
    assert observe.scope_label(scoped) == "acme/shop#5"


class _ScriptedObserver:
    """A deterministic observer stand-in for the follow loop.

    The loop's cycle, termination and failure contract is the subject
    here, so its read is the fake's own synchronous step: the real
    observer's background thread never interleaves with the controlled
    clock (its own behavior is covered below).
    """

    def __init__(self, read, due=True):
        self._read = read
        self._due = due
        self._error = None
        self.reads = 0

    def due(self, timestamp=None):
        return self._due

    def start_read(self):
        self.reads += 1
        self._error = None
        try:
            self._read()
        except observe.READ_ERRORS as exc:
            self._error = exc
        return True

    def read_error(self):
        error, self._error = self._error, None
        return error


def test_follow_renders_bounded_cycles(capsys):
    rc = observe.follow(
        _ScriptedObserver(lambda: None), lambda: "view",
        sleep=lambda seconds: None, now=lambda: 0.0, max_cycles=3,
    )
    assert rc == 0
    assert capsys.readouterr().out.count("view") == 3


def test_follow_keeps_rendering_without_a_due_github_read(capsys):
    """A cycle that is not due starts no read: the local cycle keeps
    rendering the last complete reads."""
    observer = _ScriptedObserver(lambda: None, due=False)
    rc = observe.follow(
        observer, lambda: "view", sleep=lambda seconds: None,
        now=lambda: 0.0, max_cycles=2,
    )
    assert rc == 0
    assert observer.reads == 0
    assert capsys.readouterr().out.count("view") == 2


def test_follow_fails_fast_and_preserves_the_last_view(capsys):
    calls = {"count": 0}

    def read():
        calls["count"] += 1
        if calls["count"] > 1:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")

    errors = []
    rc = observe.follow(
        _ScriptedObserver(read), lambda: "view",
        sleep=lambda seconds: None, now=lambda: 0.0, max_cycles=5,
        on_error=lambda exc, last: errors.append((str(exc), last)),
    )
    assert rc == 1
    assert errors == [
        ("Command '['gh']' returned non-zero exit status 1.", "view"),
    ]
    assert capsys.readouterr().out.count("view") == 2


def test_follow_without_a_handler_still_fails_fast():
    def read():
        raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")

    assert observe.follow(
        _ScriptedObserver(read), lambda: "view",
        sleep=lambda seconds: None, max_cycles=None,
    ) == 1


def test_follow_fails_fast_on_a_hung_gh_read():
    """A hung gh (TimeoutExpired) is the same read failure class as a
    non-zero exit: no traceback, non-zero return."""
    def read():
        raise subprocess.TimeoutExpired(["gh"], 30)

    assert observe.follow(
        _ScriptedObserver(read), lambda: "view",
        sleep=lambda seconds: None, max_cycles=None,
    ) == 1


def test_follow_preserves_the_last_view_when_the_render_fails():
    """A failure in the rendered view keeps the last successful view."""
    views = iter(["view-1", "view-2"])

    def render():
        value = next(views, None)
        if value is None:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")
        return value

    errors = []
    rc = observe.follow(
        _ScriptedObserver(lambda: None), render, sleep=lambda seconds: None,
        now=lambda: 0.0, max_cycles=8,
        on_error=lambda exc, last: errors.append((str(exc), last)),
    )
    assert rc == 1
    assert errors == [
        ("Command '['gh']' returned non-zero exit status 1.", "view-2"),
    ]


# --- the background GitHub cycle -------------------------------------------


def _wait_until(predicate, timeout=5.0):
    """Wait for the background read, with a bounded termination guard."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("background read did not finish")


def test_wait_until_guard_fails_fast_on_a_stuck_predicate():
    """The wait helper is bounded by construction (Issue #95): a
    predicate that never becomes true fails the test instead of
    hanging."""
    with pytest.raises(AssertionError, match="background read did not finish"):
        _wait_until(lambda: False, timeout=0.01)


def test_background_read_does_not_block_the_rendered_view(tmp_path):
    """A due GitHub read runs in the background: the cycle keeps
    rendering the last complete reads, and two read cycles never
    overlap."""
    gate = threading.Event()

    def read_issues(repo):
        gate.wait(5)
        return [_issue(1)]

    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(config, scope, read_issues=read_issues)
    assert observer.start_read() is True
    assert observer.reading() is True
    # The render sees the last complete reads, not a blocked read.
    assert observer.deliveries == {}
    assert observer.start_read() is False
    gate.set()
    _wait_until(lambda: not observer.reading())
    assert [
        delivery.number for delivery in observer.deliveries["acme/shop"]
    ] == [1]
    assert observer.read_error() is None


def test_background_read_failure_surfaces_once_and_keeps_the_cycle(tmp_path):
    def read_issues(repo):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")

    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(config, scope, read_issues=read_issues)
    observer.start_read()
    _wait_until(lambda: not observer.reading())
    assert isinstance(observer.read_error(), subprocess.CalledProcessError)
    assert observer.read_error() is None
    # A failed cycle schedules no next one: only the foreground decides.
    assert observer.due() is True


def test_github_cycle_restarts_when_the_read_completed(tmp_path):
    """Completion-based timing: the next cycle is due 15 seconds after
    the read finished, never 15 seconds after it started."""
    clock = {"now": 100.0}

    def read_issues(repo):
        clock["now"] += 5.0
        return []

    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope, read_issues=read_issues, now=lambda: clock["now"],
    )
    observer.refresh()
    assert clock["now"] == 105.0
    assert observer.due(115.0) is False
    assert observer.due(120.0) is True


# --- the bounded GitHub reads of one status cycle (Issue #1668) ------------


def test_status_read_bound_is_the_existing_thirty_seconds():
    """The one bound every GitHub-backed status read carries."""
    assert observe.STATUS_READ_TIMEOUT_SECONDS == 30


def test_open_issues_complete_reads_under_the_status_bound(monkeypatch):
    calls = []

    def fake_run(command, timeout=None):
        calls.append(timeout)
        return "[]"

    monkeypatch.setattr(observe, "run_gh_read_command", fake_run)
    assert observe.open_issues_complete("acme/shop") == []
    assert calls == [observe.STATUS_READ_TIMEOUT_SECONDS]


def test_confirmed_delivery_reads_the_issue_under_the_status_bound():
    calls = []

    def read_issue(number, fields, *, repo, timeout=None):
        calls.append(timeout)
        return _issue(number)

    observe.confirmed_delivery("acme/shop", 7, read_issue=read_issue)
    assert calls == [observe.STATUS_READ_TIMEOUT_SECONDS]


def test_ready_issue_and_recent_result_read_under_the_status_bound(
        monkeypatch, tmp_path):
    """Every GitHub read of the ready decision — the claim's own scans
    (Issue #1773) and the result scan — carries the status bound."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        return "[]"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert cli.ready_issue("acme/shop", _config(tmp_path)) is None
    assert cli.recent_result("acme/shop") is None
    assert {timeout for _, timeout in calls} == {
        observe.STATUS_READ_TIMEOUT_SECONDS,
    }


def _stalling_gh(tmp_path, *, stall_subcommand):
    """A `gh` on PATH that never answers one Issue subcommand."""
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "issue" ] && [ "$2" = "{stall_subcommand}" ]; then\n'
        "  exec sleep 5\n"
        "fi\n"
        'echo "[]"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _git(*args):
    subprocess.run(
        ["git", "-c", "user.email=orbi@example.com", "-c", "user.name=orbi",
         *args],
        check=True, capture_output=True, text=True,
    )


def _local_checkout(tmp_path):
    """A checkout whose `origin` is a local bare repository: the base read
    (`git fetch origin <branch>`) runs for real and offline."""
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    _git("init", "--bare", "-b", "main", str(remote))
    _git("init", "-b", "main", str(checkout))
    (checkout / "README.md").write_text("base", encoding="utf-8")
    _git("-C", str(checkout), "add", ".")
    _git("-C", str(checkout), "commit", "-m", "base")
    _git("-C", str(checkout), "remote", "add", "origin", str(remote))
    _git("-C", str(checkout), "push", "origin", "main")
    return checkout


def _run_status(monkeypatch, tmp_path, argv, *, stall_subcommand):
    """Run the real `orbi status` entry with a stalling `gh` on PATH.

    Only the bound is shrunk: the command, the config load and scope
    selection, the observer cycle, the gh subprocess, the local git
    checkout and the failure line are the real user path.
    """
    monkeypatch.setattr(observe, "STATUS_READ_TIMEOUT_SECONDS", 1)
    bin_dir = _stalling_gh(tmp_path, stall_subcommand=stall_subcommand)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    checkout = _local_checkout(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["acme/shop"]\n'
        f'repo_dir = "{checkout}"\n',
        encoding="utf-8",
    )
    return cli.main([
        "status", "--config", str(config), *argv,
    ])


def test_status_issue_scope_bounds_a_hung_issue_view(monkeypatch, tmp_path,
                                                     capsys):
    """Acceptance: `orbi status --repo acme/shop --issue 42` with a
    `gh issue view` that never answers exits 1 within the bound, with
    the scope named."""
    rc = _run_status(
        monkeypatch, tmp_path, ["--repo", "acme/shop", "--issue", "42"],
        stall_subcommand="view",
    )
    assert rc == 1
    failure = [
        line for line in capsys.readouterr().err.splitlines()
        if line.startswith("status_failed")
    ]
    assert len(failure) == 1
    assert failure[0].startswith("status_failed scope=acme/shop#42 reason=")
    assert "timed out after 1 seconds" in failure[0]


def test_status_repo_scope_bounds_a_hung_ready_list(monkeypatch, tmp_path,
                                                    capsys):
    """Acceptance: repository scope with a ready-queue `gh issue list`
    that never answers exits 1 within the bound, with the scope named."""
    rc = _run_status(
        monkeypatch, tmp_path, ["--repo", "acme/shop"],
        stall_subcommand="list",
    )
    assert rc == 1
    failure = [
        line for line in capsys.readouterr().err.splitlines()
        if line.startswith("status_failed")
    ]
    assert len(failure) == 1
    assert failure[0].startswith("status_failed scope=acme/shop reason=")
    assert "timed out after 1 seconds" in failure[0]


# --- Issue #1669: a maintainer's confirmed manual wait wins ---------------


def test_is_active_is_false_for_a_maintainer_blocked_delivery():
    """A confirmed manual wait wins over the delivery label (Issue #1669):
    the merge gate leaves ai-pr-opened in place while ai-blocked stops the
    delivery, exactly as delivery_scene.classify orders BLOCKED before
    every opened-PR state."""
    assert observe.is_active(
        observe.delivery_from_issue(
            "acme/shop", _issue(42, labels=("ai-blocked", "ai-pr-opened")),
        ),
    ) is False


def test_read_active_deliveries_excludes_a_maintainer_blocked_delivery():
    raw = [
        _issue(1),
        _issue(42, labels=("ai-blocked", "ai-pr-opened")),
    ]
    deliveries = observe.read_active_deliveries(
        "acme/shop", read_issues=lambda repo: raw,
    )
    assert [delivery.number for delivery in deliveries] == [1]


def test_render_excludes_a_maintainer_blocked_delivery_from_active(tmp_path):
    """Acceptance #1669: orbi status shows 'active: -' for a repository
    whose only labelled delivery is a maintainer-blocked Issue."""
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope,
        read_issues=lambda repo: [
            _issue(42, title="Fix login",
                   labels=("ai-blocked", "ai-pr-opened")),
        ],
    )
    observer.refresh()
    report = cli.status_report(config, scope, observer)
    assert "  active: -" in report
    assert "#42" not in report


def test_render_issue_scope_shows_a_blocked_delivery_with_its_observations(
    tmp_path,
):
    """Acceptance #1669: orbi status --issue 42 keeps the confirmed labels
    and the retained, timed observations of a blocked delivery."""
    session = (
        tmp_path / ".worktrees" / "orbi-acme-shop-issue-42-86b57234"
        / ".pi-session"
    )
    session.mkdir(parents=True)
    (session / "s.jsonl").write_text(
        json.dumps({
            "type": "session", "id": "sess-42",
            "timestamp": "2026-10-15T06:00:00Z",
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-10-15T06:20:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "read",
                 "arguments": {"path": "src/login.ts"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos, issue=42)
    observer = observe.StatusObserver(
        config, scope,
        read_issue=lambda number, fields, *, repo, timeout=None: _issue(
            number, title="Fix login", url="u42",
            labels=("ai-blocked", "ai-pr-opened"),
        ),
    )
    observer.refresh()
    report = cli.status_report(config, scope, observer)
    assert (
        "  issue: #42 Fix login u42 state=open "
        "labels=ai-blocked,ai-pr-opened"
    ) in report
    assert (
        "run=86b57234 session=sess-42 at=2026-10-15T06:20:00Z "
        "kind=read target=src/login.ts"
    ) in report
