#!/usr/bin/env python3
"""The message language: one typed setting, one message catalog (Issue #1660).

Orbi's own GitHub comments are written in the run's bound message
language. The setting has exactly two values, `zh` (Simplified Chinese)
and `en` (English), and is named for Orbi's MESSAGES rather than for
comments because the same setting will also choose the language of
Orbi's notifications (the Telegram adapter already reads it). There is no
runtime `auto` and no guessing from an Issue, a contributor, a browser or
a changed OS locale.

Precedence is the existing configuration/policy binding, per key:

* an explicit `message_language` in the repository's `.github/orbi.toml`
  on the default branch wins;
* otherwise the instance host `orbi.toml`;
* otherwise `en`.

A repository key that is absent inherits -- nothing is copied from the host
or from the example. An unsupported explicit value reports its source, key,
value and the allowed values (Article 4.5 fail fast).

The module owns: the values and their validation, the locale seeding
`orbi setup` uses for a NEW host config, the one flat message catalog, and
the instruction appended at the model-session boundary so a custom prompt
template receives the same effective language instruction.

It is deliberately NOT a framework or a translation service: no locale
database, no plural/gender machinery, no runtime translation of arbitrary
prose. A message is one key with an English and a Chinese rendering; the
renderers keep their structure (markers, machine fields, enums) intact and
translate only the human prose.
"""
from __future__ import annotations

import os
import re
from typing import Mapping

LANGUAGE_EN = "en"
LANGUAGE_ZH = "zh"
LANGUAGES = (LANGUAGE_EN, LANGUAGE_ZH)
DEFAULT_LANGUAGE = LANGUAGE_EN
MESSAGE_LANGUAGE_KEY = "message_language"

# The locale variables `orbi setup` reads, in POSIX precedence order. The
# language signal is these strings only: Python's numeric locale
# (`locale.getdefaultlocale`, `setlocale`) is never consulted -- it is not
# available in a clean sandbox and answers a different question.
LOCALE_VARIABLES = ("LC_ALL", "LC_MESSAGES", "LANG")


class LanguageError(ValueError):
    """An explicit message_language value is not `zh` or `en`."""


def allowed_values() -> str:
    """The allowed values as a quoted, comma-separated list."""
    return ", ".join(repr(value) for value in LANGUAGES)


def normalize_language(value: object, *, source: str) -> str:
    """Validate one explicit setting; report source, key, value, allowed.

    The returned value is always one of :data:`LANGUAGES`. Anything else --
    a wrong type, an unknown string, an empty string -- raises
    :class:`LanguageError` naming the file it came from, the key, the
    offending value and the allowed values, so the reader knows exactly
    what to fix and where.
    """
    if isinstance(value, str) and value in LANGUAGES:
        return value
    raise LanguageError(
        f"{source}: {MESSAGE_LANGUAGE_KEY} must be one of "
        f"{allowed_values()}; got {value!r}"
    )


def of(config: object) -> str:
    """The effective language bound to one config object.

    The single reading point for a runner config: the host validation
    already guarantees a supported value, and a hand-built (test) config
    that never bound the field keeps the pre-#1660 default instead of
    raising.
    """
    value = getattr(config, MESSAGE_LANGUAGE_KEY, DEFAULT_LANGUAGE)
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def language_from_locale(environ: Mapping[str, str] | None = None) -> str:
    """The initial language for a NEW host config, from the user's locale.

    `LC_ALL`, `LC_MESSAGES`, `LANG` in that order: the first one set to a
    non-empty value decides. A `zh` territory tag (`zh`, `zh_CN`,
    `zh-CN.UTF-8`, `zh_TW@euro`) is Chinese; every other value -- and no
    value at all, the container/`C` case -- is English.
    """
    env = os.environ if environ is None else environ
    for name in LOCALE_VARIABLES:
        value = env.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        # Strip the codeset and modifier before comparing: `zh_CN.UTF-8`
        # and `zh_TW@euro` are Chinese, `C.UTF-8` is not.
        tag = value.strip().split(".", 1)[0].split("@", 1)[0].lower()
        if tag == "zh" or tag.startswith("zh_") or tag.startswith("zh-"):
            return LANGUAGE_ZH
        return LANGUAGE_EN
    return DEFAULT_LANGUAGE


# The message-language line of an example config, as bytes: an
# UNCOMMENTED `message_language = ...` in the example is a human's explicit
# choice, a commented one is the documented default.
_MESSAGE_LANGUAGE_SET_RE = re.compile(rb"^[ \t]*message_language[ \t]*=",
                                      re.MULTILINE)
_MESSAGE_LANGUAGE_COMMENTED_RE = re.compile(
    rb"^[ \t]*#[ \t]*message_language[ \t]*=[^\n]*", re.MULTILINE,
)


def seed_config(payload: bytes,
                environ: Mapping[str, str] | None = None) -> bytes:
    """Seed a NEW host config's `message_language` from the locale.

    An uncommented value in the example (the deployment-local adjacent
    example or the packaged one) is an explicit human choice and is
    preserved untouched. Otherwise the created config carries the locale
    language: the commented default line is activated when the example
    has one, and the line is appended when it does not. Only a config
    that is being CREATED is rewritten -- an existing orbi.toml,
    including one missing the key, is never touched.
    """
    if _MESSAGE_LANGUAGE_SET_RE.search(payload):
        return payload
    seeded = language_from_locale(environ)
    line = f'{MESSAGE_LANGUAGE_KEY} = "{seeded}"'.encode("utf-8")
    if _MESSAGE_LANGUAGE_COMMENTED_RE.search(payload):
        return _MESSAGE_LANGUAGE_COMMENTED_RE.sub(line, payload, count=1)
    separator = b"" if not payload or payload.endswith(b"\n") else b"\n"
    return payload + separator + line + b"\n"


# --- the message catalog ---------------------------------------------------
#
# One entry per message: the English rendering and the Chinese rendering,
# both `str.format` templates for the fields the caller passes. Keys are
# `area.name`. A renderer translates ONLY the human prose of a comment:
# markers, machine fields, JSON keys, enums, paths, commands, URLs, error
# codes, labels and raw logs are never catalog entries.
MESSAGES: dict[str, tuple[str, str]] = {
    # The human review checklist (the ai-human-review gate).
    "human_review.title": ("**Orbi human review checklist**", "**Orbi 人工复核清单**"),
    "human_review.method": (
        "How to review: read the PR's full diff ({pr_url}), run the repository's own test suite locally, then confirm the two columns below.",
        "复核方式：阅读 PR 的完整 diff（{pr_url}），在本地跑仓库测试，再确认下面两栏。",
    ),
    "human_review.column1": (
        "Column 1 · what the AI verified (evidence, not a to-do):",
        "栏一 · AI 已验证（证据，不是待办）：",
    ),
    "human_review.column1_empty": ("(no readable automated-verification evidence)", "（无可读的自动化验证证据）"),
    "human_review.column2": (
        "Column 2 · what a person still confirms (the minimal machine-unverifiable set):",
        "栏二 · 需要人确认（机器验不了的最小集合）：",
    ),
    "human_review.confirm": (
        "Once it looks right, add the `{label}` label to this Issue and Orbi resumes the review and merge on the next tick; until then the delivery waits here (run_id={run_id}).",
        "确认无误后给本 Issue 打 `{label}` 标签，Orbi 会在下一个 tick 继续评审与合并；在此之前交付在此等待（run_id={run_id}）。",
    ),
    "human_review.column2_empty": (
        "Column 2 · what a person still confirms: **no human intervention needed** — this delivery carries no item the machine cannot verify, so the `{label}` label is not required (run_id={run_id}).",
        "栏二 · 需要人确认：**无需人工介入** —— 本次交付没有识别出机器验不了的条目，不要求 `{label}` 标签（run_id={run_id}）。",
    ),
    "human_review.item_test_result": ("Test result: PR CI {status}", "测试结果：PR CI {status}"),
    "human_review.item_coverage": (
        "Coverage layer: the repository's own test suite — automated assertions cover the code and interface layers; {count} files changed",
        "覆盖层：仓库测试套件 —— 自动化断言覆盖到代码与接口层；改动共 {count} 个文件",
    ),
    "human_review.item_intent": (
        "Intent judgment: automated tests can only show that the implementation matches the tests, never that the tests match the intent — whether this change matches the original expectation needs a person",
        "业务意图判断：自动化测试只能证明实现与测试一致，不能证明测试与意图一致——本次改动是否符合原始预期，需要人确认",
    ),
    "human_review.item_ui": ("UI look and wording: tests cannot assert rendered output or phrasing; a person confirms them in the real interface", "UI 视觉与文案：测试断言不到渲染效果与措辞，需要人在真实界面确认"),
    "human_review.item_deploy": ("External-environment verification: deployment / CI / monitoring changes must be verified in the real environment, which local tests cannot cover", "外部环境验证：部署 / CI / 监控类改动要在真实环境验证，本机测试覆盖不到"),
    "human_review.item_evidence": ("Missing test evidence: no readable test result was recorded, so the automated verification level of this delivery is unknown", "测试证据缺失：没有可读的测试结果记录，本次交付的自动化验证程度未知"),
    "human_review.item_diff": ("Unreadable changed-file list: what this delivery actually changed cannot be determined, so the verification scope is unknown", "改动文件清单不可读：无法确定本次交付实际改了什么，验证范围未知"),
    # The merge handoff (a ready PR blocked by repository policy).
    "handoff.headline": (
        "Orbi: PR #{pr_number} is delivered and waiting for a maintainer "
        "action.",
        "Orbi：PR #{pr_number} 已交付，等待维护者处理。",
    ),
    "handoff.policy_heading": (
        "blocked by this repository policy:",
        "以下仓库策略阻塞了合并：",
    ),
    "handoff.next_step": (
        "After the named policy action, Orbi will resume and merge the PR.",
        "完成上述策略要求的操作后，Orbi 会继续并合并该 PR。",
    ),

    # Review rounds and findings.
    "review.round_heading": (
        "Orbi review round {round} for PR #{pr_number}: "
        "{blockers} blocker(s), {majors} major(s).",
        "Orbi 评审第 {round} 轮，PR #{pr_number}："
        "{blockers} 个 blocker、{majors} 个 major。",
    ),
    "review.findings_heading": ("### Findings", "### 评审发现"),
    "review.findings_milestone": ("review findings: round {round}, {blockers} blocker(s), {majors} major(s) for PR #{pr_number}", "评审发现：第 {round} 轮，PR #{pr_number} 有 {blockers} 个 blocker、{majors} 个 major"),
    "review.findings_outcome": ("**Orbi review findings**\n\nround {round}: {blockers} blocker(s), {majors} major(s); the next review session retries the same PR automatically", "**Orbi 评审发现**\n\n第 {round} 轮：{blockers} 个 blocker、{majors} 个 major；下一个评审 session 会自动重试同一个 PR"),
    "review.delivered_outcome": ("**Orbi delivered**\n\nPR {pr_url} merged (merge_commit={merge_commit} review_rounds={round})", "**Orbi 已交付**\n\nPR {pr_url} 已合并（merge_commit={merge_commit} review_rounds={round}）"),
    "review.base_advance_heading": ("Orbi base advance retry {round} for PR #{pr_number}:", "Orbi base 同步重试第 {round} 次，PR #{pr_number}："),
    "review.gate_ci_blocked": ("CI merge gate blocked: {message} (run_id={run_id})", "CI 合并门禁被阻塞：{message}（run_id={run_id}）"),
    "review.gate_blocked": ("Merge gate blocked: {message} (run_id={run_id})", "合并门禁被阻塞：{message}（run_id={run_id}）"),
    "review.gate_next_step": ("The next review session merges the latest origin/{base_branch} into the branch in-session, resolves conflicts, and reruns the full test suite", "下一个评审 session 会在会话内把最新的 origin/{base_branch} 合并进分支、解决冲突，并重跑完整测试套件"),
    "review.gate_exhausted": ("base-advance retry budget exhausted; a human must resolve the hot base before this PR can continue.", "base 同步重试预算已用尽；必须由人工处理这个频繁变动的 base，该 PR 才能继续。"),
    "review.absorb_violation": (" Absorb contract violated (machine-checked): the round started with the head already behind origin/{base_branch} and ended with verdict=pass while the head still does not contain origin/{base_branch} — the session neither merged the base in-session nor reported the abandoned absorb as findings; the next review session must merge origin/{base_branch} into the branch or emit findings stating the attempted-and-abandoned absorb with the concrete reason, never an unrelated push", " 违反 absorb 约定（机器校验）：本轮开始时 head 已落后于 origin/{base_branch}，结束时 verdict=pass 但 head 仍不包含 origin/{base_branch} —— session 既没有在会话内合并 base，也没有把放弃的 absorb 作为 findings 上报；下一个评审 session 必须把 origin/{base_branch} 合并进分支，或给出声明“尝试并放弃 absorb”及具体原因的 findings，绝不能改成无关的推送"),
    "review.external_pr_round": ("Orbi external PR review round {round} for PR #{pr_number}: verdict={verdict}, blockers={blockers}, majors={majors}, minors={minors}; CI={state} ({checks}).", "Orbi 外部 PR 评审第 {round} 轮，PR #{pr_number}：verdict={verdict}, blockers={blockers}, majors={majors}, minors={minors}；CI={state}（{checks}）。"),
    "review.review_report_open": ("<details>\n<summary>Review report</summary>\n\n", "<details>\n<summary>评审报告</summary>\n\n"),
    "review.external_conclusion": ("The engine does not merge external PRs and does not set a Milestone: a maintainer decides whether to accept this contribution and which version it ships in. The Issue waits at ai-blocked until then (run_id={run_id})", "引擎不会合并外部 PR，也不会设置 Milestone：是否接受这份贡献、以及它随哪个版本发布，由维护者决定。在此之前该 Issue 保持 ai-blocked 等待（run_id={run_id}）"),
    "review.same_findings": (
        "Findings are the same as round {round}.",
        "发现与第 {round} 轮相同。",
    ),
    "review.finding_level": ("Level:", "级别："),
    "review.finding_location": ("Location:", "位置："),
    "review.finding_note": ("Note:", "说明："),
    "review.finding_fix": ("Fix:", "修复："),

    # The live progress comment and the terminal outcome.
    "progress.header": ("**Orbi progress**", "**Orbi 进度**"),
    "progress.run_details": ("Run details", "运行详情"),
    "progress.milestone": ("Orbi:", "Orbi："),
    "progress.field.role": ("role", "角色"),
    "progress.field.step": ("step", "阶段"),
    "progress.field.reported_at": ("reported at", "报告时间"),
    "progress.field.last_activity": ("last activity", "最近活动"),
    "progress.field.activity": ("activity", "活动"),
    "progress.field.tests": ("tests", "测试"),
    "progress.field.pr": ("PR", "PR"),
    "progress.field.recovery": ("recovery", "恢复状态"),
    "progress.field.review_round": ("review/fix round", "评审/修复轮次"),
    "progress.field.issue": ("issue", "Issue"),
    "progress.field.priority": ("priority", "优先级"),
    "progress.field.phase": ("phase", "阶段状态"),
    "progress.field.elapsed": ("elapsed", "已用时间"),
    "progress.field.branch": ("branch", "分支"),
    "progress.field.session": ("session", "会话"),
    "progress.outcome_headline": (
        "**Orbi {outcome} — {disposition}**",
        "**Orbi {outcome} —— {disposition}**",
    ),
    "progress.outcome_blocked": ("blocked", "已阻塞"),
    "progress.outcome_fix_needed": ("fix needed", "需要修复"),
    "progress.disposition_blocked": (
        "waiting on a human decision", "等待人工决定",
    ),
    "progress.disposition_retry": (
        "the engine will retry", "引擎会自动重试",
    ),
    "progress.default_reason_blocked": (
        "Orbi could not complete the delivery because a required "
        "operation failed.",
        "Orbi 无法完成交付，因为某个必需的操作失败了。",
    ),
    "progress.default_reason_retry": (
        "Orbi found a problem that must be fixed before it can continue.",
        "Orbi 发现了一个必须先修复的问题，然后才能继续。",
    ),
    "progress.what_to_do": (
        "What you need to do: {action}", "您需要做的操作：{action}",
    ),
    "progress.nothing": ("Nothing", "无需操作"),
    "progress.what_happened": ("What happened: {reason}", "发生了什么：{reason}"),
    "progress.what_happened_missing": (
        "What happened: No reason was provided.",
        "发生了什么：没有提供原因。",
    ),
    "progress.what_next": ("What Orbi will do next: {action}", "Orbi 接下来会做什么：{action}"),
    "progress.engine_wait_pr": (
        "Orbi will wait for the required action; the open PR is {pr_url}.",
        "Orbi 会等待所需的操作；当前打开的 PR 是 {pr_url}。",
    ),
    "progress.engine_wait_human": (
        "Orbi will wait for a human to resolve this Issue.",
        "Orbi 会等待人工处理这个 Issue。",
    ),
    "progress.raw_error": (
        "<details><summary>Raw error</summary>\n{diagnosis}\n</details>",
        "<details><summary>原始错误</summary>\n{diagnosis}\n</details>",
    ),
    "claim.scene_missing": ("Orbi failed: {error}; no trusted 'Orbi opened PR' scene comment exists on this Issue, so the opened-PR delivery cannot be resumed — this is an external precondition the AI cannot safely judge or fix, so it cannot be recovered automatically (the Issue stays ai-blocked until a human decides) — restore the trusted 'Orbi opened PR' scene comment or relabel the Issue ai-fix-needed", "Orbi 失败：{error}；这个 Issue 上没有可信的 'Orbi opened PR' 场景评论，因此无法继续已开启 PR 的交付——这是 AI 无法安全判断或修复的外部前提条件，不能自动恢复（Issue 会保持 ai-blocked，直到人工决定）——请恢复可信的 'Orbi opened PR' 场景评论，或把 Issue 重新标记为 ai-fix-needed"),
    "progress.recoverable_model_wait": ("model_wait recovered", "model_wait 已恢复"),
    "progress.recoverable_pi_failure": ("Pi failure recovered", "Pi 失败已恢复"),
    "milestone.plan_ready": ("plan ready", "计划已就绪"),
    "progress.recoverable_scene": (
        "{run_marker}\n{recovered_marker}\n"
        "Orbi {recoverable_name}: {reason_code}; retrying on the next "
        "tick\n\n"
        "The Issue stays ai-in-progress and the next tick resumes the "
        "same run ({run_info}).\n\n"
        "<details><summary>{diagnosis}</summary>\n\n"
        "- reason_code: {reason_code}\n"
        "- action: {action_code}\n"
        "- retry_safe: {retry_safe}\n\n"
        "{detail}\n\n"
        "</details>",
        "{run_marker}\n{recovered_marker}\n"
        "Orbi {recoverable_name}：{reason_code}；下一个 tick 自动重试\n\n"
        "Issue 保持 ai-in-progress，下一个 tick 会继续同一个运行（{run_info}）。\n\n"
        "<details><summary>{diagnosis}</summary>\n\n"
        "- reason_code：{reason_code}\n"
        "- action：{action_code}\n"
        "- retry_safe：{retry_safe}\n\n"
        "{detail}\n\n"
        "</details>",
    ),
    # Release-task status comments.
    "release.started": ("**Orbi release started**: {run_info}", "**Orbi release 已开始**：{run_info}"),
    "release.gates_passed": ("**Orbi release gates passed**: {evidence}", "**Orbi release 门禁已通过**：{evidence}"),
    "release.scope_verified": ("**Orbi release scope verified**: {evidence}", "**Orbi release 发布范围已验证**：{evidence}"),
    "release.tests_passed": ("**Orbi release tests passed**: {evidence}", "**Orbi release 测试已通过**：{evidence}"),
    "release.released": ("**Orbi released**: {release_url}", "**Orbi 已发布**：{release_url}"),
    "release.released_headline": ("Orbi released: {release_url}", "Orbi 已发布：{release_url}"),
    "release.failed_headline": ("Orbi release failed (ai-blocked)", "Orbi release 失败（ai-blocked）"),
    "release.section_scope": ("## Scope (verified item by item)", "## 发布范围（逐项验证）"),
    "release.section_gates": ("## Pre-release gates", "## 发布前门禁"),
    "release.section_tests": ("## Tests", "## 测试"),
    "release.section_milestone": ("## Milestone", "## Milestone"),
    "release.yielded": ("Orbi release yielded: a concurrent release instance already pushed the same version commit\ntag: {tag}\nbase_branch: {base_branch}\nremote_commit: {remote_commit}\nrun_id={run_id}", "Orbi release 已让出：另一个并发 release 实例已经推送了同一个版本提交\ntag: {tag}\nbase_branch: {base_branch}\nremote_commit: {remote_commit}\nrun_id={run_id}"),
    "release.waiting": ("Orbi release waiting for deliveries", "Orbi release 正在等待其他交付完成"),

    # Runner-owned scene/status comments outside the progress comment.
    "runner.external_pr_routed": ("Orbi: the `ai-pr-opened` state of this triage Issue comes from external contribution PR #{pr_number} (state: {state}), not from a runner scene; routing it through the external takeover review on the next claim instead of blocking it.", "Orbi：这个 triage Issue 的 `ai-pr-opened` 状态来自外部贡献 PR #{pr_number}（state: {state}），而不是 runner 场景；下次认领时会走外部接管评审，而不是把它阻塞掉。"),
    "runner.ticket_only_delivered": (
        "Orbi ticket-only delivery (run_id={run_id}):",
        "Orbi 仅工单交付（run_id={run_id}）：",
    ),
    "runner.ticket_only_milestone": (
        "ticket-only delivered: {run_info}",
        "仅工单交付完成：{run_info}",
    ),
    "runner.ticket_only_outcome": (
        "**Orbi ticket-only delivered**",
        "**Orbi 仅工单交付完成**",
    ),
    "runner.ticket_only_failed": (
        "Orbi ticket-only failed: {detail} ({run_info})\n"
        "run_id={run_id}\n"
        "No Git branch, commit, or PR was created.",
        "Orbi 仅工单交付失败：{detail}（{run_info}）\n"
        "run_id={run_id}\n"
        "没有创建 Git 分支、提交或 PR。",
    ),
    "runner.ticket_only_blocked_milestone": (
        "ticket-only blocked: {detail} ({run_info})",
        "仅工单交付已阻塞：{detail}（{run_info}）",
    ),
    "runner.ops_milestone": (
        "ops delivered: {run_info}", "ops 交付完成：{run_info}",
    ),
    "runner.ops_outcome": (
        "**Orbi ops delivered**", "**Orbi ops 交付完成**",
    ),
    "runner.delivery_stopped_reason": (
        "The delivery stopped before it could be completed: {detail}",
        "交付在完成之前停止了：{detail}",
    ),
    "runner.pr_closed_milestone": ("blocked: PR {pr_url} was closed without a merge; the delivery is terminally failed", "已阻塞：PR {pr_url} 未合并就被关闭；本次交付最终失败"),
    "runner.pr_closed_reason": ("PR {pr_url} was closed without a merge; the delivery is terminally failed", "PR {pr_url} 未合并就被关闭；本次交付最终失败"),
    "runner.pr_closed_next_step": ("investigate why the PR was closed and re-open the delivery or start a fresh run on the Issue", "请查明该 PR 被关闭的原因，然后重新开启交付或在 Issue 上开始一次新的运行"),
    "runner.issue_closed": (
        "Orbi delivery stopped: the Issue was closed while the delivery "
        "was in flight; the completed work stays on branch \u0060{branch}\u0060 "
        "and no PR was opened.\nrun_id={run_id}",
        "Orbi 交付已停止：交付进行中该 Issue 被关闭；已完成的工作保留在分支 "
        "\u0060{branch}\u0060 上，没有创建 PR。\nrun_id={run_id}",
    ),

    # The run's started-Pi / opened-PR headlines (the run scenes).
    "delivery.started_headline": ("Orbi started Pi:", "Orbi 已启动 Pi："),
    "delivery.opened_pr_headline": ("Orbi opened PR:", "Orbi 已开启 PR："),
    "delivery.merged_headline": ("Orbi merged PR: {pr_url}", "Orbi 已合并 PR：{pr_url}"),
    "delivery.review_row": ("- review: {review}", "- 评审：{review}"),
    "delivery.review_rounds": (" ({rounds} review rounds)", "（{rounds} 个评审轮次）"),
    "delivery.merged_into": ("- merged into: {base_branch}", "- 合并目标：{base_branch}"),
    "review.summary_human_action": ("maintainer-actionable merge retry", "需要维护者处理的合并重试"),
    "review.summary_pass_recorded": ("pass recorded on {head}; merged after CI", "在 {head} 上记录通过；CI 之后合并"),
    "review.summary_pass_clean": ("pass, no findings", "通过，没有发现"),
    "review.summary_pass_fixed": ("pass, {findings} findings fixed in-session", "通过，会话内修复了 {findings} 项发现"),

    # Delivery failures (blocked / fix needed).
    "failure.headline_fix_needed": (
        "Orbi: fix needed — the engine will retry",
        "Orbi：需要修复 —— 引擎会自动重试",
    ),
    "failure.headline_blocked": (
        "Orbi: blocked — waiting on a human decision",
        "Orbi：已阻塞 —— 等待人工决定",
    ),
    "failure.pr_line": ("PR: [{pr_url}]({pr_url})", "PR：[{pr_url}]({pr_url})"),
    "failure.issue_line": (
        "Issue: `{issue}` · run_id={run_id}",
        "Issue：`{issue}` · run_id={run_id}",
    ),
    "failure.action_label": ("**Action:**", "**需要您做的操作：**"),
    "failure.reason_label": ("**Reason:**", "**原因：**"),
    "failure.disposition_needs_fix": ("Orbi needs a fix", "Orbi 需要修复"),
    "failure.disposition_failed": ("Orbi failed", "Orbi 失败"),
    "failure.diagnosis_summary": ("Diagnosis", "诊断"),
    "failure.disposition_row": (
        "- disposition: `{disposition}: see the reason above`",
        "- disposition：`{disposition}：原因见上方`",
    ),
    "failure.correlation_row": (
        "- correlation: `{correlation}`",
        "- correlation：`{correlation}`",
    ),
    "failure.detail_row": (
        "- failure detail: `{detail}`",
        "- failure detail：`{detail}`",
    ),
    "failure.detail_row_same": (
        "- failure detail: `{disposition}: see the reason above`",
        "- failure detail：`{disposition}：原因见上方`",
    ),
    "failure.command_failed": (
        "the delivery command failed",
        "交付命令执行失败",
    ),
    "failure.terminal_unclassified": (
        "The cause could not be determined from the failure evidence; "
        "review it and re-run this Issue once the cause is fixed.",
        "无法从失败证据中确定原因；请查看证据，修复原因后重新运行这个 Issue。",
    ),
    "failure.terminal_generic": (
        "Fix the failure described below and re-run this Issue.",
        "请修复下面描述的失败，然后重新运行这个 Issue。",
    ),
    "failure.workflow_cause": (
        "GitHub rejected the push because the current GitHub App "
        "credential cannot modify Actions workflows.",
        "GitHub 拒绝了这次推送：当前的 GitHub App 凭据无法修改 Actions "
        "workflow。",
    ),
    "failure.workflow_action_pr": (
        "{cause} Check the App's installation settings for a permission "
        "update that includes Workflows read/write. After resolving the "
        "permission, remove `ai-blocked` and add `ai-fix-needed` to "
        "continue PR {pr_url}. If no update is available or the same error "
        "persists, contact the App operator.",
        "{cause}。请检查该 App 安装设置里是否可授予包含 Workflows 读写权限的"
        "权限更新。权限解决后，移除 `ai-blocked` 并添加 `ai-fix-needed`，"
        "继续 PR {pr_url}。如果无法更新权限或同样的错误仍然出现，请联系 App "
        "的运维者。",
    ),
    "failure.workflow_action_no_pr": (
        "{cause} Check the App's installation settings for a permission "
        "update that includes Workflows read/write. After resolving the "
        "permission, remove `ai-blocked` and ensure `ai-ready` is present "
        "to run this Issue again. If no update is available or the same "
        "error persists, contact the App operator. No PR has been created.",
        "{cause}。请检查该 App 安装设置里是否可授予包含 Workflows 读写权限的"
        "权限更新。权限解决后，移除 `ai-blocked` 并确保存在 `ai-ready`，"
        "以便重新运行这个 Issue。如果无法更新权限或同样的错误仍然出现，"
        "请联系 App 的运维者。本次没有创建 PR。",
    ),
    "failure.blocked_precondition": (
        "this is an external precondition the AI cannot safely judge or "
        "fix, so it cannot be recovered automatically (the Issue stays "
        "ai-blocked until a human decides)",
        "这是 AI 无法安全判断或修复的外部前提条件，因此无法自动恢复"
        "（Issue 会保持 ai-blocked，直到人工决定）",
    ),
    "failure.scene_blocked": (
        "Orbi failed: {error}; this is an external precondition the AI "
        "cannot safely judge or fix, so it cannot be recovered "
        "automatically (the Issue stays ai-blocked until a human decides) "
        "— restore the trusted 'Orbi opened PR' scene comment or relabel "
        "the Issue ai-fix-needed to resume this same PR",
        "Orbi 失败：{error}；这是 AI 无法安全判断或修复的外部前提条件，"
        "因此无法自动恢复（Issue 会保持 ai-blocked，直到人工决定）——"
        "请恢复可信的 'Orbi opened PR' 场景评论，或把 Issue 重新标记为 "
        "ai-fix-needed 以继续同一个 PR",
    ),
    "failure.repo_config_blocked": (
        "Orbi failed: repository config is invalid: {error}; this is a "
        "fail-fast repository precondition the AI does not fix itself — "
        "correct the file on the default branch (remove the host-only key "
        "or fix the value) and relabel the Issue ai-ready for a new run",
        "Orbi 失败：仓库配置无效：{error}；这是快速失败的仓库前提条件，"
        "AI 不会自行修复——请在默认分支上修正该文件（移除 host-only 键或"
        "修正取值），然后把 Issue 重新标记为 ai-ready 以开始新的一次运行",
    ),
    "failure.resume_blocked": (
        "Orbi failed: cannot continue the interrupted run: {detail} "
        "(run_id={run_id})",
        "Orbi 失败：无法继续被中断的运行：{detail}（run_id={run_id}）",
    ),
    "failure.comment_truncated": (
        "\n\n_[comment truncated at {chars} chars; full session log: "
        "{log}]_",
        "\n\n_[评论在 {chars} 字符处被截断；完整会话日志：{log}]_",
    ),
    "failure.milestone_blocked": (
        "blocked: {summary}", "已阻塞：{summary}",
    ),
    "failure.milestone_fix_needed": (
        "fix needed: {summary}", "需要修复：{summary}",
    ),
    "failure.next_step_blocked_classified": (
        "fix the precondition above (see the reason) and relabel the "
        "Issue ai-fix-needed to resume this same PR",
        "修复上面的前提条件（见原因），然后把 Issue 重新标记为 "
        "ai-fix-needed 以继续同一个 PR",
    ),
    "failure.next_step_blocked_terminal": (
        "fix the failure above and re-run this Issue (a new run id is "
        "created automatically)",
        "修复上面的失败并重新运行这个 Issue（会自动创建新的 run id）",
    ),
    "failure.next_step_fix_needed": (
        "the next tick resumes the same run, branch, worktree and PR "
        "automatically (the Issue stays ai-fix-needed)",
        "下一个 tick 会自动继续同一个 run、分支、工作树和 PR"
        "（Issue 保持 ai-fix-needed）",
    ),
    "failure.scene.run": ("run", "运行"),
    "failure.scene.issue": ("issue", "Issue"),
    "failure.scene.role": ("role", "角色"),
    "failure.scene.branch": ("branch", "分支"),
    "failure.scene.worktree": ("worktree", "工作树"),
    "failure.scene.session": ("session", "会话"),
    "failure.scene.session_log": ("session log", "会话日志"),
    "failure.scene.phase": ("phase", "阶段状态"),
    "failure.scene.last_activity": ("last activity", "最近活动"),
    "failure.scene.action": ("action", "动作"),
    "failure.scene.result": ("result", "结果"),
    "failure.scene.legacy_correlation": ("legacy correlation", "旧版关联信息"),
    "failure.scene.local_worktree": (
        "local runner worktree", "本机 Runner 工作树",
    ),
    "failure.scene.local_session_log": (
        "local session log", "本机会话日志",
    ),
    "failure.scene.unavailable": ("<unavailable>", "<不可用>"),
    "failure.evidence.heading": (
        "Failure evidence (captured before cleanup):",
        "失败证据（清理前采集）：",
    ),
    "failure.evidence.session_last_events": (
        "session_last_events (last {limit} records; full log: {label}):",
        "session_last_events（最近 {limit} 条记录；完整日志：{label}）：",
    ),
    # Clarify gate (thin tickets).
    "clarify.heading": (
        "**Orbi: this ticket is not ready to deliver.**",
        "**Orbi：这张工单还不能交付。**",
    ),
    "clarify.intro": (
        "The pre-flight check says the Issue does not state pieces a "
        "delivery needs:",
        "预检发现这个 Issue 没有写清交付所需的要素：",
    ),
    "clarify.repair": (
        "Edit the Issue body into this shape, then add the "
        "`{ready_label}` label again:",
        "请按下面的形状补充 Issue 正文，然后重新加上 `{ready_label}` 标签：",
    ),
    "clarify.repair_outcome": (
        "Outcome: what a user does, and what they should see differently.",
        "Outcome：用户要做什么，以及应该看到什么变化。",
    ),
    "clarify.repair_acceptance": (
        "Acceptance: how a finished delivery is told apart from an "
        "unfinished one.",
        "Acceptance：如何判断交付已完成、而不是半成品。",
    ),
    "clarify.followup": (
        "Orbi re-checks the ticket when it claims it and starts the "
        "delivery once the pieces are there. Until then the ticket "
        "carries `{needs_detail_label}` and waits for you.",
        "Orbi 在认领这张工单时会重新检查，要素补齐后立即开始交付。"
        "在那之前工单保持 `{needs_detail_label}`，等待您补充信息。",
    ),
    "clarify.missing.observable_result": (
        "No observable result: the ticket never says what a user sees or "
        "does differently once the change ships.",
        "缺少可观察结果：工单没有说明改动上线后用户会看到或做到什么不同的事。",
    ),
    "clarify.missing.acceptance_condition": (
        "No acceptance condition: there is no concrete way to tell a "
        "finished delivery from an unfinished one.",
        "缺少验收条件：没有具体办法判断交付是否已完成。",
    ),
    "clarify.missing.single_outcome": (
        "More than one outcome: the ticket asks for several unrelated "
        "things instead of one fix or one change.",
        "包含多个结果：工单要求的是若干互不相关的事情，而不是一个修复或一个改动。",
    ),
}


def text(language: str, key: str, **fields: object) -> str:
    """Render one catalog message in one language.

    The language is the run's bound value; anything but `zh` renders the
    English entry, so a hand-built (test) config that never bound the
    field keeps the exact pre-#1660 output.
    """
    english, chinese = MESSAGES[key]
    template = chinese if language == LANGUAGE_ZH else english
    return template.format(**fields) if fields else template


# --- the model-session boundary -------------------------------------------
#
# The instruction is appended to the rendered system prompt AT THE SESSION
# BOUNDARY (never inside a shipped template), so a custom prompt template
# receives exactly the same effective instruction. It applies only to the
# comment-facing natural-language fields the session produces (questions,
# summary, note, fix): code, paths, commands, URLs, error codes, raw logs,
# JSON keys, enum values, labels and test names stay verbatim.
_INSTRUCTION_EN = (
    "## Message language\n\n"
    "Some natural-language fields this session produces (questions, "
    "summary, notes, fix explanations) are published directly into GitHub "
    "comments by Orbi. Write those fields in English -- nothing else "
    "changes. Keep code, paths, commands, URLs, error codes, raw logs, "
    "JSON keys, enum values such as verdict/severity/action/kind, labels "
    "and test names exactly as they are; never translate them."
)
_INSTRUCTION_ZH = (
    "## 消息语言\n\n"
    "本次会话产生的一部分自然语言字段（提问、总结、说明、修复解释）会被 Orbi "
    "直接发布到 GitHub 评论。请用简体中文撰写这些字段，其余一切不变。代码、"
    "路径、命令、URL、错误码、原始日志、JSON 键、verdict/severity/action/kind "
    "等枚举值、标签和测试名称保持原样，不要翻译。"
)


def model_instruction(language: str) -> str:
    """The session-boundary language instruction for one language."""
    body = _INSTRUCTION_ZH if language == LANGUAGE_ZH else _INSTRUCTION_EN
    return "\n\n" + body


def with_model_instruction(system_prompt: str, language: str) -> str:
    """Append the boundary instruction to one rendered system prompt."""
    return system_prompt + model_instruction(language)
