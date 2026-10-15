"""Behavioral tests for the scoped orbi-status observation (Issue #1656).

The settled contract lives in docs/public-contracts.mdx; these tests
exercise the observable behavior with synthetic repositories, JSONL
events, GitHub responses and controlled time: scope/selection, exact
attribution, active-set membership, confirmed removal, the two read
cycles, bounded termination and the failure path.
"""
import json
import subprocess
from pathlib import Path

import pytest

import orbi.status_observe as observe
from orbi.config import RunnerConfig


def _issue(number, *, title="task", url="u", state="open",
           labels=("ai-in-progress",), pull_request=None):
    issue = {
        "number": number, "title": title, "url": url, "state": state,
        "labels": [{"name": name} for name in labels],
    }
    if pull_request is not None:
        issue["pull_request"] = pull_request
    return issue


def _config(tmp_path, repos=("acme/shop",)):
    return RunnerConfig(
        source_repos=tuple(repos), repo_dir=tmp_path, base_branch="main",
        max_concurrency=1, slot_dir=tmp_path / ".orbi" / "slots",
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

    def read_issue(number, fields, *, repo):
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


def test_safe_target_rejects_a_configured_excluded_path(tmp_path):
    excluded = str((tmp_path / "secrets" / "token").resolve())
    assert observe.safe_target(
        tmp_path, "secrets/token", exclude_paths=frozenset({excluded}),
    ) is None


# --- activity classification -----------------------------------------------


def test_activity_of_classifies_read_with_target():
    record = _assistant([
        {"type": "toolCall", "name": "read_file",
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
        {"type": "toolCall", "name": "read_file", "arguments": "x"},
    ])
    assert observe.activity_of(record) == ("read", None)


def test_activity_of_read_tool_without_a_path_key():
    record = _assistant([
        {"type": "toolCall", "name": "read_file", "arguments": {"other": 1}},
    ])
    assert observe.activity_of(record) == ("read", None)


def test_activity_of_classifies_command_without_target():
    record = _assistant([
        {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}},
    ])
    assert observe.activity_of(record) == ("command", None)


def test_activity_of_classifies_unknown_tool_as_analysis():
    record = _assistant([{"type": "toolCall", "name": "webfetch"}])
    assert observe.activity_of(record) == ("analysis", None)


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
                {"type": "toolCall", "name": "read_file",
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
                {"type": "toolCall", "name": "read_file",
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
        read_issue=lambda number, fields, *, repo: _issue(
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

    def read_issue(number, fields, *, repo):
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


def test_follow_renders_bounded_cycles(tmp_path, capsys):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope, read_issues=lambda repo: [], now=lambda: 0.0,
    )
    rc = observe.follow(
        observer, lambda: "view",
        sleep=lambda seconds: None, now=lambda: 0.0, max_cycles=3,
    )
    assert rc == 0
    assert capsys.readouterr().out.count("view") == 3


def test_follow_fails_fast_and_preserves_the_last_view(tmp_path, capsys):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)
    calls = {"count": 0}
    clock = {"now": 0.0}

    def read_issues(repo):
        calls["count"] += 1
        if calls["count"] > 1:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")
        return []

    def sleep(seconds):
        clock["now"] += 15.0

    errors = []
    observer = observe.StatusObserver(
        config, scope, read_issues=read_issues, now=lambda: 0.0,
    )
    rc = observe.follow(
        observer, lambda: "view", sleep=sleep, now=lambda: clock["now"],
        max_cycles=5,
        on_error=lambda exc, last: errors.append((str(exc), last)),
    )
    assert rc == 1
    assert errors == [
        ("Command '['gh']' returned non-zero exit status 1.", "view"),
    ]


def test_follow_without_a_handler_still_fails_fast(tmp_path):
    config = _config(tmp_path)
    scope = observe.parse_scope(config.source_repos)

    def read_issues(repo):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")

    observer = observe.StatusObserver(config, scope, read_issues=read_issues)
    assert observe.follow(
        observer, lambda: "view", sleep=lambda seconds: None, max_cycles=None,
    ) == 1
