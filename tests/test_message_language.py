"""The message language: setting, precedence, rendering, boundary (Issue #1660).

One test file for the whole contract:

* the host key and the repository policy key (validation + precedence),
* the `orbi setup` locale seeding (the adjacent example wins),
* the Runner-owned comment families in `zh`/`en` (an override does not
  leak into the machinery: markers, kinds, prefixes and readers stay
  language-independent),
* the model-session boundary instruction, which a CUSTOM prompt
  template receives too (and which the clarify ticket agent does not),
* this repository's explicit `en` ledger.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbi import clarify, comment_kind, delivery_comment, failure, scene
from orbi import human_review, language, merge_handoff, progress
from orbi import release_comments, review_rounds
from orbi.config import RunnerConfig
from orbi.delivery_scene import RunContext
from orbi.repo_config import RepoConfigError, parse_repo_config, resolve_policy

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _repository_permissions(monkeypatch):
    """Issue #1806: the commented identities here are the runner's own
    bot account, which stays trusted by the credential fallback; the
    permission read is answered too so no test reaches real GitHub."""
    from seam import seam

    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "orbi-build[bot]",
    )


def _prompt_of(command: list[str]) -> str:
    """The rendered system prompt the argv names (Issue #1634)."""
    value = command[command.index("--system-prompt") + 1]
    path = Path(value)
    return path.read_text(encoding="utf-8") if path.is_file() else value


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="abcdef12", issue=7, branch="orbi/owner-repo-issue-7",
        worktree=tmp_path, source_repo="owner/repo",
    )


# --------------------------------------------------------------- the setting

def test_language_values_are_en_and_zh_only():
    assert language.LANGUAGES == ("en", "zh")
    assert language.normalize_language("zh", source="orbi.toml") == "zh"
    assert language.normalize_language("en", source="orbi.toml") == "en"


@pytest.mark.parametrize("value", ["cn", "zh-CN", "", 1, True, None])
def test_unsupported_value_reports_source_key_value_and_allowed(value):
    with pytest.raises(language.LanguageError) as excinfo:
        language.normalize_language(value, source="/tmp/orbi.toml")
    message = str(excinfo.value)
    assert "/tmp/orbi.toml" in message
    assert "message_language" in message
    assert repr(value) in message
    assert "'en', 'zh'" in message


def test_host_config_load_carries_the_language(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "."\n'
        'message_language = "zh"\n',
        encoding="utf-8",
    )
    from orbi.config import load_config

    assert load_config(config_path).message_language == "zh"


def test_host_config_rejects_an_unsupported_language(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nlanguage = "fr"\n'
        'message_language = "fr"\n',
        encoding="utf-8",
    )
    from orbi.config import load_config

    with pytest.raises(language.LanguageError) as excinfo:
        load_config(config_path)
    assert "message_language" in str(excinfo.value)
    assert "'fr'" in str(excinfo.value)


def test_repository_policy_accepts_the_key_and_rejects_a_bad_value():
    policy = parse_repo_config('message_language = "zh"\n')
    assert policy.message_language == "zh"
    with pytest.raises(RepoConfigError) as excinfo:
        parse_repo_config('message_language = "cn"\n')
    assert ".github/orbi.toml" in str(excinfo.value)
    assert "message_language" in str(excinfo.value)


def test_repository_language_overrides_the_host_and_deleting_it_inherits():
    host = RunnerConfig(message_language="zh")
    overridden = resolve_policy(host, parse_repo_config('message_language = "en"\n'))
    assert overridden.message_language == "en"
    # An absent repository key inherits the host value -- never a copy of
    # the default, and deleting the repository key restores inheritance.
    inherited = resolve_policy(host, parse_repo_config(""))
    assert inherited.message_language == "zh"
    default_host = resolve_policy(
        RunnerConfig(), parse_repo_config(""),
    )
    assert default_host.message_language == "en"


def test_this_repository_keeps_its_english_ledger():
    """AC6: the repository declares `en`, so a Chinese HOST still writes
    this repository's comments in English (the delegated ledger policy)."""
    policy = parse_repo_config(
        (REPO_ROOT / ".github" / "orbi.toml").read_text(encoding="utf-8"),
        source=".github/orbi.toml",
    )
    assert policy.message_language == "en"
    bound = resolve_policy(RunnerConfig(message_language="zh"), policy)
    assert bound.message_language == "en"


# ----------------------------------------------------------- locale seeding

@pytest.mark.parametrize("environ,expected", [
    ({"LC_ALL": "zh_CN.UTF-8"}, "zh"),
    ({"LC_ALL": "", "LC_MESSAGES": "zh"}, "zh"),
    ({"LANG": "zh_TW@euro"}, "zh"),
    ({"LANG": "zh"}, "zh"),
    ({"LANG": "en_US.UTF-8"}, "en"),
    ({"LANG": "C"}, "en"),
    ({"LANG": "de_DE"}, "en"),
    ({}, "en"),
])
def test_locale_seeding_precedence_and_tags(environ, expected):
    assert language.language_from_locale(environ) == expected


def test_locale_seeding_only_uses_the_three_environment_variables(monkeypatch):
    """The numeric Python locale is never the signal (Issue #1660)."""
    monkeypatch.setattr(
        language.os, "environ", {"LC_CTYPE": "zh_CN.UTF-8"},
    )
    assert language.language_from_locale() == "en"


def test_seed_config_preserves_an_explicit_example_value():
    payload = b'source_repos = ["a/b"]\nmessage_language = "zh"\n'
    assert language.seed_config(
        payload, {"LANG": "en_US.UTF-8"},
    ) == payload


def test_seed_config_activates_the_commented_default():
    payload = b'source_repos = ["a/b"]\n# message_language = "en"\n'
    assert language.seed_config(payload, {"LC_ALL": "zh_CN"}) == (
        b'source_repos = ["a/b"]\nmessage_language = "zh"\n'
    )


def test_seed_config_appends_the_key_when_the_example_has_none():
    assert language.seed_config(b"x = 1\n", {}) == (
        b'x = 1\nmessage_language = "en"\n'
    )
    assert language.seed_config(b"", {}) == b'message_language = "en"\n'


def test_seed_config_keeps_the_key_top_level_before_a_trailing_table():
    """Issue #1687 acceptance 4: an example whose last block is a
    `[[repositories]]` table gets the seeded key at the TOP level. Appending
    it to the end put the key INSIDE that table, where it is ignored, and
    the created config silently fell back to `en`."""
    import tomllib

    payload = (
        b'source_repos = ["a/b"]\n'
        b'\n[[repositories]]\nname = "b"\ngithub = "a/b"\n'
    )
    seeded = language.seed_config(payload, {"LANG": "zh_CN.UTF-8"})
    assert seeded.index(b'message_language = "zh"') < seeded.index(
        b"[[repositories]]",
    )
    parsed = tomllib.loads(seeded.decode("utf-8"))
    assert parsed["message_language"] == "zh"
    assert parsed["repositories"] == [{"name": "b", "github": "a/b"}]
    assert parsed["source_repos"] == ["a/b"]


def test_the_packaged_example_documents_the_commented_default():
    example = (REPO_ROOT / "src" / "orbi" / "example_config.toml").read_text(
        encoding="utf-8",
    )
    assert '# message_language = "en"' in example


# ------------------------------------------------------- bound comment text

def test_the_explicit_language_renders(tmp_path):
    body = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0",
        language_code="zh",
    )
    assert "Orbi 已启动 Pi：" in body


def test_started_scene_follows_the_bound_language(tmp_path):
    zh = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0", language_code="zh",
    )
    assert "Orbi 已启动 Pi：" in zh
    assert comment_kind.comment_kind(zh) == comment_kind.COMMENT_KIND_STARTED
    assert progress.find_started_comment(
        [{"body": zh}], "abcdef12",
    ) is not None
    en = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0", language_code="en",
    )
    assert "Orbi started Pi:" in en


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_the_started_comment_never_names_the_runner_worktree_path(
    tmp_path, language_code,
):
    """Issue #1715: the public started comment shows the branch and the
    run id and no path on the runner host, in both languages."""
    probe = tmp_path / "orbi-tenant-probe" / "delivery"
    body = delivery_comment.started_pi_comment_body(
        RunContext(
            run_id="abc12345", issue=1153,
            branch="orbi/owner-repo-issue-1153",
            worktree=(probe / ".worktrees"
                      / "orbi-owner-repo-issue-1153-abc12345"),
            source_repo="owner/repo",
        ),
        "base_branch=main base_sha=abc123 repo_config=cfg "
        "run_id=abc12345 priority=normal session=session-1",
        language_code=language_code,
    )
    assert str(probe) not in body
    assert "worktree" not in body
    assert "run_id=abc12345" in body
    details = body.split("<details><summary>", 1)[1]
    branch_label = "分支" if language_code == "zh" else "branch"
    session_label = "会话" if language_code == "zh" else "session"
    assert f"- {branch_label}: orbi/owner-repo-issue-1153" in details
    assert f"- {session_label}: session-1" in details


def test_opened_pr_comment_keeps_the_scene_block(tmp_path):
    from orbi import scene

    zh = delivery_comment.opened_pr_comment_body(
        "abcdef12", "base_branch=main base_sha=abc run_id=abcdef12",
        "https://github.com/owner/repo/pull/1", language_code="zh",
    )
    assert "Orbi 已开启 PR：" in zh
    parsed = scene.parse(zh)
    assert parsed is not None and parsed.pr_url.endswith("/pull/1")


def test_progress_body_labels_and_header_follow_the_language(tmp_path):
    state = {"run_id": "abcdef12", "issue": 7, "issue_title": "t",
             "language": "zh", "role": "implement", "priority": "p0",
             "step": "implement", "tests": "-", "pr": None,
             "phase": "starting", "elapsed": "0s"}
    body = progress.progress_body(state)
    assert "**Orbi 进度**" in body
    assert "- 角色: 实现" in body
    assert "<details><summary>运行详情</summary>" in body
    assert body.startswith("<!-- orbi:run=abcdef12 -->")
    # The documented formal prefix and the machine block are untouched.
    assert progress._has_progress_prefix(body, progress.run_marker("abcdef12"))
    assert progress.find_progress_comment(
        [{"body": body}], "abcdef12",
    ) is not None


def test_recoverable_scene_body_follows_the_language():
    record = failure.Failure(
        reason_code="unclassified", action_code="fix_ticket",
        retry_safe=False, outcome="blocked",
    )
    zh = progress.recoverable_scene_body(
        run_id="abcdef12", recoverable_name="model_wait 已恢复", record=record,
        detail="boom", run_info="run_id=abcdef12", language_code="zh",
    )
    assert "下一个 tick 自动重试" in zh
    assert progress.RECOVERED_MARKER_PATTERN.search(zh) is not None


def test_terminal_outcome_body_follows_the_language():
    zh = progress._finish_outcome_body(
        outcome="blocked", detail="d", next_step="s", pr_url=None,
        number=7, source_repo="owner/repo", action="a", reason="r",
        diagnosis="dg", language_code="zh",
    )
    assert "**Orbi 已阻塞" in zh
    assert "您需要做的操作：a" in zh
    assert "发生了什么：r" in zh


def test_classified_failure_comment_follows_the_language():
    zh = failure.failure_comment_body(
        outcome="fix needed", action="do it", reason="boom", diagnosis="",
        scene="- 分支: `b`\n- 会话: `s`", evidence="", pr_url=None,
        issue="owner/repo#7", run_id="abcdef12", language_code="zh",
    )
    assert "Orbi：需要修复" in zh
    assert "**原因：**" in zh
    assert "run=abcdef12 branch=b session=s" in zh


def test_problem_comments_end_with_the_community_help_line():
    """Issue #1684: a "something went wrong, a person must act" message
    ends with the one community-help line; a non-problem message does
    not."""
    en = language.text("en", "help.community")
    zh = language.text("zh", "help.community")
    assert en == (
        "Need help? Join the Orbi community: https://t.me/+kJRTTpFGBRU0M2Y9"
    )
    assert zh == (
        "需要帮助？加入 Orbi 用户群：https://t.me/+kJRTTpFGBRU0M2Y9"
    )
    # The blocked failure diagnostic no longer ends with it here: the ONE
    # blocked exit appends it to every blocked comment (Issue #1730), so the
    # renderer and that exit can never both add it (the exit's own test
    # asserts the posted body). Fix-needed retries never get it.
    blocked = failure.failure_comment_body(
        outcome="blocked", action="a", reason="boom", diagnosis="",
        scene="- run: `x`", evidence="", pr_url=None,
        issue="owner/repo#7", run_id="abcdef12", language_code="en",
    )
    assert not blocked.endswith(en)
    assert blocked.endswith("</details>")
    fix_needed = failure.failure_comment_body(
        outcome="fix needed", action="a", reason="boom", diagnosis="",
        scene="- run: `x`", evidence="", pr_url=None,
        issue="owner/repo#7", run_id="abcdef12", language_code="zh",
    )
    assert not fix_needed.endswith(zh)
    # The clarification comment ends with it, in its bound language.
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("single_outcome",),
    )
    issue = {"number": 7, "title": "t", "author": {"login": "x"}}
    assert clarify.render_comment(
        issue, verdict, "abcdef12", language_code="zh",
    ).rstrip("\n").endswith(zh)
    # The checklist: a non-empty column 2 ends with it, an empty one is
    # exactly as before.
    held = human_review.render_checklist_comment(
        run_id="abcdef12", pr_url="https://x/pull/1",
        checklist={"column1": ["x"], "column2": ["Intent judgment"]}, language_code="en",
    )
    assert held.endswith(en)
    clear = human_review.render_checklist_comment(
        run_id="abcdef12", pr_url="https://x/pull/1",
        checklist={"column1": ["x"], "column2": []}, language_code="en",
    )
    assert en not in clear


def test_workflow_permission_refusal_states_cause_remedy_and_label_in_both():
    for code, fragments in (
        ("en", ["GitHub rejected the push", "ai-fix-needed", "ai-blocked"]),
        ("zh", ["GitHub 拒绝了这次推送", "ai-fix-needed", "ai-blocked"]),
    ):
        reason = failure.workflow_permission_cause(code)
        action = failure.workflow_permission_action(
            "https://github.com/owner/repo/pull/1", language_code=code,
        )
        assert fragments[0] in reason
        assert fragments[1] in action and fragments[2] in action
        assert "No PR has been created" not in action
    no_pr = failure.workflow_permission_action(None, language_code="zh")
    assert "本次没有创建 PR" in no_pr


def test_terminal_action_does_not_blame_the_ticket_in_either_language():
    record = failure.classify("totally unexpected")
    for code in ("en", "zh"):
        action = failure.terminal_action(record, code)
        assert "Issue" in action or "issue" in action
    assert failure.terminal_action(record, "zh").startswith("无法从失败证据")


def test_clarify_comment_renders_the_missing_pieces_in_the_bound_language():
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result", "single_outcome"),
    )
    issue = {"number": 7, "title": "t", "author": {"login": "x"}}
    zh = clarify.render_comment(
        issue, verdict, "abcdef12", language_code="zh",
    )
    assert "请按下面的形状补充 Issue 正文" in zh
    assert "缺少可观察结果" in zh
    assert "验收：" in zh and "结果：" in zh
    en = clarify.render_comment(issue, verdict, "abcdef12", language_code="en")
    assert "Orbi: this ticket is not ready to deliver." in en


def test_review_round_comment_keeps_its_kind_and_scene_in_chinese():
    body = review_rounds.review_round_comment_body(
        progress.run_marker("abcdef12"), 2, 9, 1, 0,
        [{"level": "Blocker", "location": "src/a.py", "note": "n", "fix": "f"}],
        scene.render(scene.Scene(
            run_id="abcdef12", base_branch="main", base_sha="abc",
            pr_url="https://github.com/owner/repo/pull/9", review_round=2,
        )),
        language_code="zh", repo="owner/repo",
    )
    assert "Orbi 评审第 2 轮" in body
    assert "**级别：** Blocker" in body
    assert comment_kind.comment_kind(body) == comment_kind.COMMENT_KIND_REVIEW_ROUND
    assert review_rounds.review_rounds_so_far(
        [{"body": body, "authorAssociation": "MEMBER",
          "author": {"login": "orbi-build[bot]"}}],
        run_id="abcdef12", pr_number=9, repo="owner/repo",
    ) == 1


def test_merge_handoff_is_idempotent_across_languages():
    zh = merge_handoff.handoff_body(
        progress.run_marker("abcdef12"), pr_number=9, head="h1",
        preflight=["merge_gate: FAILED policy"], language_code="zh",
    )
    comments = [{"body": zh, "authorAssociation": "MEMBER",
                 "author": {"login": "orbi-build[bot]"}}]
    assert merge_handoff.awaiting_merge_handoff_announced(
        comments, repo="owner/repo", head="h1",
        preflight=["merge_gate: FAILED policy"],
    )
    legacy = (
        "Orbi: PR #9 is delivered and waiting for a maintainer action.\n\n"
        "PR #9 at head `h1` is blocked by this repository policy:\n"
        "````\nmerge_gate: FAILED policy\n````"
    )
    assert merge_handoff.awaiting_merge_handoff_announced(
        [{"body": legacy, "authorAssociation": "MEMBER",
          "author": {"login": "orbi-build[bot]"}}],
        repo="owner/repo", head="h1",
        preflight=["merge_gate: FAILED policy"],
    )


def test_human_review_checklist_follows_the_language():
    zh = human_review.render_checklist_comment(
        run_id="abcdef12", pr_url="https://x/pull/1",
        checklist=human_review.build_checklist(
            test_status="passed", changed_files=["src/a.py"],
            language_code="zh",
        ),
        language_code="zh",
    )
    assert "**Orbi 人工复核清单**" in zh
    assert "栏二 · 需要人确认" in zh
    assert "ai-human-review" in zh
    en = human_review.render_checklist_comment(
        run_id="abcdef12", pr_url="https://x/pull/1",
        checklist={"column1": ["x"], "column2": []}, language_code="en",
    )
    assert "no human intervention needed" in en


def test_merged_receipt_keeps_its_kind_and_fixed_rows_in_chinese():
    from orbi import review_merge

    zh = review_merge.merged_pr_comment_body(
        "abcdef12", "https://github.com/owner/repo/pull/9", "c1", 2, 1, 3,
        "main", "pass, no findings", language_code="zh",
    )
    assert "Orbi 已合并 PR：https://github.com/owner/repo/pull/9" in zh
    assert comment_kind.comment_kind(zh) == comment_kind.COMMENT_KIND_MERGED
    # The fixed machine rows survive translation (Issue #1659 contract).
    for row in ("merge_commit=c1", "review_rounds=2", "commits=3",
                "external_commits=1", "run_id=abcdef12",
                "pr_url=https://github.com/owner/repo/pull/9"):
        assert row in zh
    assert "- 评审：pass, no findings（2 个评审轮次）" in zh


def test_single_round_receipt_has_no_round_suffix():
    from orbi import review_merge

    one = review_merge.merged_pr_comment_body(
        "abcdef12", "https://x/pull/9", "c1", 1, 0, 1, "main",
        "pass, no findings", language_code="zh",
    )
    assert "评审轮次" not in one


def test_release_comments_follow_the_language():
    from orbi.failure_report import PreExistingCIFailure

    zh = release_comments.release_success_comment_body(
        "abcdef12", "run_id=abcdef12 priority=p0",
        "https://github.com/owner/repo/releases/tag/v1", "v1", "abc",
        ["scope item"], ["gate item"], "test evidence", "milestone",
        "zh",
    )
    assert "Orbi 已发布：" in zh
    assert "## 发布范围（逐项验证）" in zh
    failure_body = release_comments.release_failure_comment_body(
        "abcdef12", "run_id=abcdef12 priority=p0",
        PreExistingCIFailure("boom"), "zh",
    )
    assert "Orbi release 失败（ai-blocked）" in failure_body
    assert failure_body.startswith("<!-- orbi:run=abcdef12 -->")


def test_epic_audit_follows_the_language_with_byte_identical_english():
    """Issue #1705: the audit sentence is catalog text under `epic.`; the
    English rendering is byte-identical to the pre-#1705 literal."""
    evidence = ["PR #9 merged", "Issue #21 closed"]
    assert release_comments._epic_audit(evidence, None, "en") == (
        "Epic reconciliation: complete; children: PR #9 merged, "
        "Issue #21 closed; no open native blockers/dependencies."
    )
    assert release_comments._epic_audit(evidence, "v0.4.0", "en") == (
        "Epic reconciliation for v0.4.0: complete; children: PR #9 merged, "
        "Issue #21 closed; no open native blockers/dependencies."
    )
    versioned_zh = release_comments._epic_audit(evidence, "v0.4.0", "zh")
    assert "Epic 对账" in versioned_zh
    assert "v0.4.0" in versioned_zh
    assert "complete" not in versioned_zh
    # The child fragments are the same raw evidence in both languages.
    assert "PR #9 merged, Issue #21 closed" in versioned_zh


def test_epic_audit_posted_matches_either_language():
    """An Epic that already carries today's English audit gets no second
    comment when the next sweep renders the same sentence in Chinese."""
    evidence = ["Issue #21 closed"]
    en = release_comments._epic_audit(evidence, None, "en")
    zh = release_comments._epic_audit(evidence, None, "zh")
    assert release_comments.epic_audit_posted(
        [{"body": "run\n" + en}], evidence,
    )
    assert release_comments.epic_audit_posted(
        [{"body": "run\n" + zh}], evidence,
    )
    assert not release_comments.epic_audit_posted([], evidence)
    assert not release_comments.epic_audit_posted(
        [{"body": "Epic #20 kept open: open blockers: #3"}], evidence,
    )
    # The version is part of the rendered sentence.
    assert not release_comments.epic_audit_posted(
        [{"body": en}], evidence, "v0.4.0",
    )
    assert release_comments.epic_audit_posted(
        [{"body": release_comments._epic_audit(evidence, "v0.4.0", "zh")}],
        evidence, "v0.4.0",
    )


def test_the_run_details_summary_follows_the_language():
    """Issue #1687 item 3: the collapsed "Run details" summary of every
    field block is catalog text; it used to be an English literal, so the
    release comments stayed half-English on a zh host."""
    fields = {"session": "x" * 300}
    zh = progress.field_block("abcdef12", "head", fields, language_code="zh")
    en = progress.field_block("abcdef12", "head", fields, language_code="en")
    assert "<summary>运行详情</summary>" in zh
    assert "<summary>Run details</summary>" in en
    zh_release = release_comments.release_success_comment_body(
        "abcdef12", "run_id=abcdef12 priority=p0 session=" + "x" * 300,
        "https://github.com/owner/repo/releases/tag/v1", "v1", "abc",
        ["scope"], ["gate"], "tests", "milestone", "zh",
    )
    assert "<summary>运行详情</summary>" in zh_release
    assert "Run details" not in zh_release


def test_milestone_plan_ready_follows_the_publisher_language(tmp_path):
    published = []

    class Publisher:
        language_code = "zh"

        def milestone(self, text, **kwargs):
            published.append(text)

    from orbi import milestone as milestone_module

    (tmp_path / ".orbi").mkdir()
    (tmp_path / ".orbi" / "plan.md").write_text("plan", encoding="utf-8")
    milestone_module._publish_plan_milestone(Publisher(), tmp_path)
    assert published == ["计划已就绪"]

    class EnglishPublisher(Publisher):
        language_code = "en"

    published.clear()
    milestone_module._publish_plan_milestone(EnglishPublisher(), tmp_path)
    assert published == ["plan ready"]


# ---------------------------------------------- display text (Issue #1729)

def test_started_comment_translates_visible_rows_and_keeps_machine_rows(
        tmp_path,
):
    zh = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path),
        "base_branch=main base_sha=abc1234 repo_config=deadbeef "
        "run_id=abcdef12 priority=normal session=session-1",
        language_code="zh",
    )
    assert "Orbi 已启动 Pi： run_id=abcdef12 优先级=普通" in zh
    assert "- 基础分支: main" in zh
    assert "- 优先级: 普通" in zh
    assert "- 基础 SHA: abc1234" in zh
    assert "- 会话: session-1" in zh
    # The public machine rows are byte-identical.
    assert "- run_id=abcdef12" in zh
    assert "- repo_config: deadbeef" in zh
    en = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path),
        "base_branch=main base_sha=abc1234 repo_config=deadbeef "
        "run_id=abcdef12 priority=normal session=session-1",
        language_code="en",
    )
    assert "Orbi started Pi: run_id=abcdef12 priority=normal" in en
    assert "- base_branch: main" in en
    assert "- priority: normal" in en
    assert "- repo_config: deadbeef" in en
    assert "- base_sha: abc1234" in en


def test_chinese_started_headline_carries_no_english_priority_key(tmp_path):
    """Issue #1746: the headline's escaping label is the display label too.

    The priority keeps its place in the one-line headline with the same
    Chinese key as the row below (`优先级`); `run_id=` is a machine row
    and stays byte-identical. An English repository is unchanged.
    """
    zh = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0", language_code="zh",
    )
    headline = zh.splitlines()[2]
    assert "priority=" not in zh
    assert "run_id=abcdef12" in headline
    assert "优先级=P0" in headline
    en = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0", language_code="en",
    )
    assert en.splitlines()[2] == "Orbi started Pi: run_id=abcdef12 priority=p0"


@pytest.mark.parametrize(
    ("raw", "rendered"), [("wait", "等待"), ("term", "终止"), ("kill", "强制终止")],
)
def test_recovery_value_follows_the_language(raw, rendered):
    """Issue #1746: a stuck session's recovery row is Chinese too; the
    stored value never leaks into a Chinese comment, the English row is
    unchanged."""
    state = {
        "run_id": "abcdef12", "issue": 7, "issue_title": "t",
        "role": "implement", "priority": "normal", "step": "implement",
        "tests": "passed", "pr": None, "phase": "bash", "elapsed": "12s",
        "activity": None, "recovery": raw,
    }
    zh = progress.progress_body({**state, "language": "zh"})
    assert f"- 恢复状态: {rendered}" in zh
    assert raw not in zh
    en = progress.progress_body({**state, "language": "en"})
    assert f"- recovery: {raw}" in en


def test_the_workflow_docs_name_the_rows_a_chinese_run_shows():
    """Issue #1746 AC3: the zh page names each repo-config row exactly as
    a Chinese repository renders it (label from the ONE catalogue), and
    the en page says a Chinese repository shows translated row names."""
    from orbi import progress_display

    zh_docs = (REPO_ROOT / "docs" / "zh" / "workflow.mdx").read_text(
        encoding="utf-8",
    )
    for key in ("repo_config_ignored", "repo_config_changed",
                "repo_config_diff"):
        assert f"「{progress_display.field_label('zh', key)}」" in zh_docs
    en_docs = (REPO_ROOT / "docs" / "workflow.mdx").read_text(
        encoding="utf-8",
    )
    assert "message language" in en_docs
    assert "中文仓库" not in en_docs
    for key in ("repo_config_ignored", "repo_config_changed",
                "repo_config_diff"):
        assert key in en_docs
    assert "忽略的仓库配置键" in en_docs


def test_opened_pr_comment_translates_visible_rows():
    zh = delivery_comment.opened_pr_comment_body(
        "abcdef12",
        "base_branch=main base_sha=abc1234 run_id=abcdef12 priority=p0",
        "https://github.com/owner/repo/pull/1", language_code="zh",
    )
    assert "Orbi 已开启 PR： https://github.com/owner/repo/pull/1" in zh
    assert "- 基础分支: main" in zh
    assert "- 优先级: P0" in zh
    assert "- run_id=abcdef12" in zh


def test_merged_milestone_translates_headline_and_field_labels():
    calls = []

    def run_command(command, **kwargs):
        calls.append(command)
        return json.dumps({"id": 42, "body": "x", "url": "u"})

    publisher = progress.ProgressPublisher(
        7, "owner/repo", "abcdef12", run_command=run_command,
        language_code="zh",
    )
    publisher.milestone(
        "merged: https://github.com/owner/repo/pull/1 "
        "merge_commit=m1 review_rounds=1 external_commits=0 commits=1",
    )
    body = calls[0][-1]
    assert "Orbi：已合并" in body
    assert "- 结果: https://github.com/owner/repo/pull/1" in body
    assert "- 评审轮次: 1" in body
    assert "- 外部提交: 0" in body
    assert "- 提交数: 1" in body
    assert "- 合并提交: m1" in body
    assert "- run_id=abcdef12" in body


def test_progress_values_follow_the_language():
    state = {
        "run_id": "abcdef12", "issue": 7, "issue_title": "t",
        "language": "zh", "role": "review", "priority": "normal",
        "step": "merge", "tests": "passed", "pr": None,
        "phase": "bash", "elapsed": "12s",
        "activity": {"kind": "analysis", "target": None},
    }
    body = progress.progress_body(state)
    assert "- 角色: 评审" in body
    assert "- 阶段: 合并" in body
    assert "- 活动: 分析" in body
    assert "- 测试: 通过" in body
    assert "- 优先级: 普通" in body
    assert "- 阶段状态: 命令行" in body
    assert "- 状态: pending" not in body


def test_failed_tests_keep_the_external_check_names_raw():
    state = {
        "run_id": "abcdef12", "issue": 7, "issue_title": "t",
        "language": "zh", "role": "implement", "priority": "p0",
        "step": "implement", "tests": "failed: ci / test", "pr": None,
        "phase": "read", "elapsed": "1s", "activity": None,
    }
    body = progress.progress_body(state)
    assert "- 测试: 失败：ci / test" in body


def test_english_milestone_and_started_rows_are_unchanged():
    calls = []

    def run_command(command, **kwargs):
        calls.append(command)
        return json.dumps({"id": 42, "body": "x", "url": "u"})

    publisher = progress.ProgressPublisher(
        7, "owner/repo", "abcdef12", run_command=run_command,
        language_code="en",
    )
    publisher.milestone(
        "merged: https://github.com/owner/repo/pull/1 "
        "merge_commit=m1 review_rounds=1 external_commits=0 commits=1",
    )
    body = calls[0][-1]
    assert "Orbi: merged" in body
    assert "- result: https://github.com/owner/repo/pull/1" in body
    assert "- review_rounds: 1" in body
    assert "- external_commits: 0" in body
    assert "- commits: 1" in body
    assert "- merge_commit: m1" in body
    assert "- run_id=abcdef12" in body


def test_zh_contract_rows_are_byte_identical(tmp_path):
    """Issue #1729 AC4: the machine rows stay exactly as v0.7.2."""
    started = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path),
        "base_branch=main base_sha=abc1234 repo_config=deadbeef "
        "run_id=abcdef12 priority=normal",
        language_code="zh",
    )
    assert "- repo_config: deadbeef" in started
    assert "- run_id=abcdef12" in started
    assert "<!-- orbi:run=abcdef12 -->" in started
    assert "<!-- orbi:comment=started -->" in started
    from orbi.failure_report import PreExistingCIFailure
    from orbi import release_comments

    failed = release_comments.release_failure_comment_body(
        "abcdef12", "run_id=abcdef12 priority=normal",
        PreExistingCIFailure("boom"), "zh",
    )
    assert "- failure: boom" in failed
    assert "<!-- orbi:failure:v1 " in failed


def test_merged_receipt_reads_naturally_without_findings():
    from orbi import review_merge

    review = language.text("zh", "review.summary_pass_clean")
    assert review == "通过，没有发现问题"
    zh = review_merge.merged_pr_comment_body(
        "abcdef12", "https://github.com/owner/repo/pull/9", "c1", 2, 1, 3,
        "main", review, language_code="zh",
    )
    assert "- 评审：通过，没有发现问题（2 个评审轮次）" in zh


def test_pr_attribution_footer_follows_the_language():
    link = "https://orbi.build/?ref=prfoot-owner"
    zh = delivery_comment.pr_attribution_footer(
        7, link, language_code="zh",
    )
    assert zh == language.text(
        "zh", "runner.pr_attribution", link=link, issue=7,
    )
    assert "由 [Orbi]" in zh and "#7" in zh and link in zh
    en = delivery_comment.pr_attribution_footer(
        7, link, language_code="en",
    )
    assert en == f"Built by [Orbi]({link}) from Issue #7"


# ------------------------------------------------------ the session boundary

def test_the_model_instruction_is_appended_to_a_custom_template():
    from orbi import pi_session

    template = "CUSTOM SYSTEM PROMPT {{RUN_ID}}"
    prompt = language.with_model_instruction(
        pi_session.render_prompt(template, {"RUN_ID": "abcdef12"}), "zh",
    )
    assert prompt.startswith("CUSTOM SYSTEM PROMPT abcdef12")
    assert "## 消息语言" in prompt
    assert "verdict/severity/action/kind" in prompt


def test_run_pi_and_run_review_append_the_instruction(monkeypatch, tmp_path):
    from orbi import pi_session

    prompts: list[str] = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: prompts.append(_prompt_of(command)) or "ok",
    )
    monkeypatch.setattr(pi_session, "apply_runner_runtime_excludes", lambda *_: None)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("SYSTEM {{RUN_ID}}", encoding="utf-8")
    review = tmp_path / "prompt_review.md"
    review.write_text("REVIEW {{ROUND}}", encoding="utf-8")
    config = RunnerConfig(
        prompt=prompt, prompt_review=review, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), workspace_root=tmp_path, skills=(),
        base_branch="main", base_sha="abc123def456", run_id="abcdef12",
        message_language="zh",
    )
    ctx = pi_session_run_context(tmp_path)
    pi_session.run_pi({"number": 7, "title": "t", "body": "b"}, ctx, config)
    pi_session.run_review(
        ctx,
        {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, 1,
    )
    assert len(prompts) == 2
    for rendered in prompts:
        assert "## 消息语言" in rendered


def test_the_clarify_ticket_agent_stays_protocol_only(monkeypatch, tmp_path):
    """AC4: the thin-ticket gate keeps its JSON protocol -- no language
    instruction is added to the shared no-tools ticket agent."""
    from orbi import pi_session

    captured: list[str] = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: captured.append(_prompt_of(command)) or "ok",
    )
    config = RunnerConfig(
        repo_dir=tmp_path, source_repos=("owner/repo",), skills=(),
        run_id="abcdef12", message_language="zh",
    )
    pi_session.run_ticket_agent(
        {"number": 7, "title": "t", "body": "b"}, config, "owner/repo",
        system_prompt=clarify.CLARIFY_SYSTEM_PROMPT,
        context=clarify.build_context({"number": 7, "title": "t", "body": "b"}),
    )
    assert captured == [clarify.CLARIFY_SYSTEM_PROMPT]
    assert "消息语言" not in captured[0]


def pi_session_run_context(tmp_path: Path) -> RunContext:
    return _ctx(tmp_path)


# -------------------------------------------- language-independent machinery

def test_mixed_history_stays_readable(tmp_path):
    """AC3: a Chinese comment and a historical English one are both read
    by the same readers (kinds first, then the English fallback)."""
    old = (
        "<!-- orbi:run=abcdef12 -->\n\n**Orbi progress**\n\n"
        "- role: review\n"
    )
    new = progress.progress_body({
        "run_id": "abcdef12", "issue": 7, "issue_title": "t",
        "language": "zh", "role": "review", "priority": "p0",
        "phase": "starting", "elapsed": "0s",
    })
    found = progress.find_progress_comment(
        [{"body": old}, {"body": new}], "abcdef12",
    )
    assert found is not None and found["body"] == new
    old_started = (
        "<!-- orbi:run=abcdef12 -->\n\nOrbi started Pi: run_id=abcdef12\n"
    )
    zh_started = delivery_comment.started_pi_comment_body(
        _ctx(tmp_path), "run_id=abcdef12 priority=p0", language_code="zh",
    )
    assert progress.find_started_comment(
        [{"body": old_started}], "abcdef12",
    ) is not None
    assert progress.find_started_comment(
        [{"body": zh_started}], "abcdef12",
    ) is not None
    assert progress.format_status_comment(new).startswith(new.split(chr(10))[0])


def test_failure_folding_ignores_the_rendered_language():
    from orbi import progress as progress_module

    zh = failure.failure_comment_body(
        outcome="fix needed", action="a", reason="boom", diagnosis="",
        scene="", evidence="", pr_url=None, issue="owner/repo#7",
        run_id="abcdef12", language_code="zh",
    )
    body = (progress_module.run_marker("abcdef12") + chr(10)
            + progress_module.failure_marker("0123456789abcdef") + chr(10) + zh)
    assert progress_module.failure_repeat_count(body) == 1
    assert progress_module.bump_failure_repeat(body, "0123456789abcdef").count(
        "0123456789abcdef:2",
    ) == 1


# ---------------------------------------------- the one unblock step (#1724)
#
# The step a blocked Issue needs to get back into the delivery loop: the
# claim scans skip `ai-blocked`, so it is removed BEFORE `ai-fix-needed`
# goes on. It is written ONCE in the catalog and composed by every message
# that tells a person how to resume -- the two scene messages had drifted
# (`or relabel`), because each carried its own copy of the step.
UNBLOCK_STEP = {
    "en": "remove `ai-blocked`, then add `ai-fix-needed`",
    "zh": "移除 `ai-blocked`，再添加 `ai-fix-needed`",
}

RESUME_MESSAGES = (
    "review.blocked_rounds_action",
    "review.blocked_human_action",
    "review.blocked_generic_action",
    "failure.workflow_action_pr",
    "failure.next_step_blocked_classified",
    "failure.scene_blocked",
    "claim.scene_missing",
)

PR_URL = "https://github.com/owner/repo/pull/9"

SCENE_RECOVERY = {
    "en": (
        "restore the trusted 'Orbi opened PR' scene comment, then "
        + UNBLOCK_STEP["en"]
    ),
    "zh": (
        "请恢复可信的 'Orbi opened PR' 场景评论，然后" + UNBLOCK_STEP["zh"]
    ),
}


@pytest.mark.parametrize(
    "index, step", [(0, UNBLOCK_STEP["en"]), (1, UNBLOCK_STEP["zh"])],
)
def test_the_unblock_step_is_written_once_in_the_catalog(index, step):
    """Issue #1724 acceptance 3: one catalog entry owns the resume step;
    no other message carries its own copy that could drift again."""
    assert language.MESSAGES["blocked.unblock_step"][index] == step
    owners = [
        key for key, pair in language.MESSAGES.items() if step in pair[index]
    ]
    assert owners == ["blocked.unblock_step"]


@pytest.mark.parametrize("key", RESUME_MESSAGES)
def test_every_resume_message_composes_the_one_unblock_step(key):
    """Issue #1724 acceptance 3: the review-stage blocked actions, the
    workflow-permission action, the classified next step and the two scene
    messages all compose the shared entry instead of restating it."""
    for index in (0, 1):
        assert "{unblock_step}" in language.MESSAGES[key][index]
    for code, step in UNBLOCK_STEP.items():
        rendered = language.text(
            code, key, pr_url=PR_URL, fix="fix", error="boom",
            cause="cause",
        )
        assert rendered.count(step) == 1


@pytest.mark.parametrize("code", ["en", "zh"])
@pytest.mark.parametrize(
    "key", ["claim.scene_missing", "failure.scene_blocked"],
)
def test_scene_missing_messages_give_the_one_working_unblock_path(key, code):
    """Issue #1724 acceptance 1: both scene messages say to restore the
    trusted scene comment, THEN remove `ai-blocked`, THEN add
    `ai-fix-needed` -- the only path that works -- and offer no
    "or relabel" alternative."""
    body = language.text(code, key, error="scene comment gone")
    assert SCENE_RECOVERY[code] in body
    # The `ai-fix-needed` label appears only inside the shared step: no
    # alternative relabel branch survives.
    assert "ai-fix-needed" not in body.replace(UNBLOCK_STEP[code], "")
    assert " or relabel" not in body
    assert "或把 Issue 重新标记" not in body


# The failed alternative the two scene messages carried: "restore the scene
# comment OR relabel the Issue". It reached the reader through a message
# template AND through a reviewer-facing exception detail, so the guard scans
# the whole engine, not just the catalog.
RELABEL_ALTERNATIVES = ("or relabel the Issue", "或把 Issue 重新标记")


@pytest.mark.parametrize("phrase", RELABEL_ALTERNATIVES)
def test_no_module_carries_the_relabel_alternative(phrase):
    """Issue #1724 acceptance 1/3: a blocked comment names ONE working
    resume path. The failed alternative must not survive as a second copy
    in a template or in a failure detail a comment publishes."""
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "src" / "orbi").glob("*.py"))
    )
    assert phrase not in sources
