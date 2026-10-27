"""The display vocabulary of Orbi's run comments (Issue #1729).

Split out of `language.MESSAGES` so the catalog owner stays under the
size ratchet's 1,000-line cap. Every key here is a display label or a
stored display value; `language.MESSAGES` merges this map in place, so
`language.text()` / `language.term()` remain the ONE reading point. No
machine row (run marker, `run_id=`, `repo_config:`, `failure:`, receipt
rows) has an entry here.
"""

DISPLAY_TERMS: dict[str, tuple[str, str]] = {
    # Display labels of the started / opened-PR scene field blocks. The
    # machine rows (`run_id=`, `repo_config:`) have no entry here and stay
    # verbatim (Issue #1729).
    "progress.field.base_branch": ("base_branch", "基础分支"),
    "progress.field.base_sha": ("base_sha", "基础 SHA"),
    "progress.field.task_type": ("task_type", "任务类型"),
    "progress.field.external": ("external", "外部"),
    "progress.field.repo_config_ignored": (
        "repo_config_ignored", "忽略的仓库配置键",
    ),
    "progress.field.repo_config_changed": (
        "repo_config_changed", "仓库配置变更",
    ),
    "progress.field.repo_config_diff": ("repo_config_diff", "仓库配置差异"),
    # Display labels of the merged milestone's visible rows.
    "progress.field.result": ("result", "结果"),
    "progress.field.merge_commit": ("merge_commit", "合并提交"),
    "progress.field.review_rounds": ("review_rounds", "评审轮次"),
    "progress.field.external_commits": ("external_commits", "外部提交"),
    "progress.field.commits": ("commits", "提交数"),
    # The display vocabulary of the progress values (Issue #1729): the
    # stored value stays the raw machine word, the rendered one follows
    # the message language. An unknown value renders verbatim.
    "progress.role.implement": ("implement", "实现"),
    "progress.role.review": ("review", "评审"),
    "progress.role.release": ("release", "发布"),
    "progress.role.ticket": ("ticket", "工单"),
    "progress.role.suggest": ("suggest", "建议"),
    "progress.step.starting": ("starting", "启动中"),
    "progress.step.implement": ("implement", "实现"),
    "progress.step.review": ("review", "评审"),
    "progress.step.checks": ("checks", "检查"),
    "progress.step.merge": ("merge", "合并"),
    "progress.step.release": ("release", "发布"),
    "progress.priority.p0": ("p0", "P0"),
    "progress.priority.normal": ("normal", "普通"),
    "progress.tests.passed": ("passed", "通过"),
    "progress.tests.pending": ("pending", "等待中"),
    "progress.tests.failed": ("failed: {names}", "失败：{names}"),
    # The progress comment's recovery row while a session is stuck
    # (Issue #1746): the stored state stays `wait`/`term`/`kill`.
    "progress.recovery.wait": ("wait", "等待"),
    "progress.recovery.term": ("term", "终止"),
    "progress.recovery.kill": ("kill", "强制终止"),
    "progress.activity.read": ("read", "读取"),
    "progress.activity.edit": ("edit", "编辑"),
    "progress.activity.command": ("command", "命令"),
    "progress.activity.analysis": ("analysis", "分析"),
    "progress.phase.starting": ("starting", "启动中"),
    "progress.phase.session_pending": ("session_pending", "等待会话"),
    "progress.phase.request_pending": ("request_pending", "等待响应"),
    "progress.phase.read": ("read", "读取"),
    "progress.phase.edit": ("edit", "编辑"),
    "progress.phase.write": ("write", "写入"),
    "progress.phase.bash": ("bash", "命令行"),
    "progress.phase.pr": ("pr", "PR"),
    "progress.phase.push": ("push", "推送"),
    "progress.phase.commit": ("commit", "提交"),
    "progress.phase.base": ("base", "基线"),
    "progress.phase.worktree": ("worktree", "工作树"),
    "progress.phase.ui": ("ui", "界面"),
    "progress.task_type.ticket-only": ("ticket-only", "仅工单"),
    "progress.task_type.ops": ("ops", "运维"),
    "progress.boolean.true": ("true", "是"),
    "progress.boolean.false": ("false", "否"),
    # The merged milestone's display headline. It is the ONLY milestone
    # whose event word arrives raw: every other milestone text is already
    # a localized sentence from `language.text`, so an entry for its
    # English word would never be reached (Issue #1729).
    "progress.milestone_headline.merged": ("merged", "已合并"),
}

# The base-branch-red scene's own sentences (Issue #1825). They live here
# for the same reason DISPLAY_TERMS does: the catalog owner stays under the
# size cap, and `language.MESSAGES` merges this map in, so `language.text`
# remains the ONE reading point. Every key is prose a human reads; the check
# names, branches, SHAs, PR numbers and URLs inside them stay verbatim.
BASE_CI_RED_MESSAGES: dict[str, tuple[str, str]] = {
    # The base-branch-red scene (Issue #1825): a delivery PR that is ready
    # but whose failing check is also red on the base branch. The comment
    # leads with what the user already has (a ready PR), names why it
    # paused, shows every failing base check with its run link and first
    # error lines, and offers the two repairs. Names, URLs, SHAs and check
    # names stay verbatim in both languages.
    "base_ci_red.ready": (
        "✅ **PR #{pr} is ready:** review passed, tests pass.",
        "✅ **PR #{pr} 已就绪：** review 通过，测试通过。",
    ),
    "base_ci_red.paused": (
        "⛔ **Paused** because the base branch `{base_branch}` is "
        "already failing {checks} (not caused by this PR).",
        "⛔ **已暂停**：基础分支 `{base_branch}` 上的 {checks} "
        "本来就是失败的（不是这个 PR 造成的）。",
    ),
    "base_ci_red.check_run": (
        "- **`{name}`** — failing run: {url}",
        "- **`{name}`** —— 失败的 run：{url}",
    ),
    "base_ci_red.check_errors": (
        "- **`{name}`** — first error lines:",
        "- **`{name}`** —— 最初的错误行：",
    ),
    "base_ci_red.next_steps": (
        "**Next steps** (pick one):",
        "**下一步**（选一种）：",
    ),
    "base_ci_red.next_step_fix_base": (
        "1. Fix `{base_branch}` yourself — Orbi merges this PR "
        "automatically once that check is green. No relabel needed.",
        "1. 自己修好 `{base_branch}` —— 那个 check 变绿后 Orbi 会"
        "自动合并这个 PR，不需要改标签。",
    ),
    "base_ci_red.next_step_command": (
        "2. Or comment `/orbi fix-base` (requires write access) and "
        "Orbi opens a fix Issue for it.",
        "2. 或者评论 `/orbi fix-base`（需要 write 权限），由 Orbi "
        "开一张修复 Issue。",
    ),
    "base_ci_red.state_resuming": (
        "✅ `{base_branch}` is green again — Orbi is resuming this "
        "delivery automatically.",
        "✅ `{base_branch}` 已经变绿 —— Orbi 正在自动恢复这次交付。",
    ),
    "base_ci_red.state_merged": (
        "✅ Merged: `{base_branch}` turned green and Orbi merged "
        "this PR.",
        "✅ 已合并：`{base_branch}` 变绿后 Orbi 自动合并了这个 PR。",
    ),
    "base_ci_red.state_stale": (
        "⏰ `{base_branch}` is still failing {checks} after {days} "
        "days — Orbi stops checking this PR. Fix the base branch, or "
        "comment `/orbi fix-base`, then remove `ai-blocked` "
        "and add `ai-fix-needed` to resume.",
        "⏰ `{base_branch}` 上的 {checks} 已经连续 {days} 天失败 —— "
        "Orbi 停止检查这个 PR。请修好基础分支，或评论 `/orbi fix-base`，"
        "然后去掉 `ai-blocked`、加上 `ai-fix-needed` 恢复。",
    ),
    "base_ci_red.triage_reason": (
        "A delivery PR is paused because this check fails on the base "
        "branch, not in the PR. Once it is green on `{base_branch}` "
        "(commit `{base_sha}` is where it was first seen), Orbi "
        "merges that PR automatically — no relabel needed.",
        "有一个交付 PR 被暂停，因为失败的 check 出现在基础分支上，而不是这个 "
        "PR 里。等 `{base_branch}` 上这个 check 变绿（首次发现时的"
        "提交是 `{base_sha}`），Orbi 会自动合并那个 PR —— 不需要改"
        "标签。",
    ),
    "base_ci_red.triage_check": (
        "## Failing check: {name} on {base_branch}",
        "## 失败的 check：{base_branch} 上的 {name}",
    ),
    "base_ci_red.triage_errors": (
        "First error lines:",
        "最初的错误行：",
    ),
    "base_ci_red.triage_created": (
        "created #{number} — `{check}` on `{base_branch}`",
        "已创建 #{number} —— `{base_branch}` 上的 `{check}`",
    ),
    "base_ci_red.triage_reused": (
        "reused #{number} — `{check}` on `{base_branch}`",
        "已复用 #{number} —— `{base_branch}` 上的 `{check}`",
    ),
    # The command's own precondition (Issue #1825): the shared
    # dispatcher evaluates this command on every ticket that runs
    # commands, and only a paused base-branch-red delivery carries the
    # block the handler needs.
    "command.reason_fix_base_no_block": (
        "`/orbi fix-base` only works on a delivery paused by a red "
        "base branch; no `orbi:base-ci-red:v1` block is present here",
        "`/orbi fix-base` 只对因基础分支已红而暂停的交付有效；这里没有 "
        "`orbi:base-ci-red:v1` 块",
    ),
    "base_ci_red.triage_no_evidence": (
        "No failing check evidence is available for `{base_branch}` "
        "any more; fix the base branch and Orbi resumes this PR "
        "automatically.",
        "`{base_branch}` 上已读不到失败 check 的证据；修好基础分支后 "
        "Orbi 会自动恢复这个 PR。",
    ),
}

# The missing check-read permission scene (Issue #1918): the App
# installation has not approved a permission the check read needs, and the
# delivery stops with one comment naming the permission and the approval
# page. Same reason as the two maps above: the catalog owner stays under the
# size cap and `language.text` remains the ONE reading point. The permission
# name (`statuses: read`), the URLs and the PR numbers stay verbatim.
CHECK_PERMISSION_MESSAGES: dict[str, tuple[str, str]] = {
    "check_permission.open": (
        "🔒 **PR #{pr} is open, but Orbi cannot read its check results.**",
        "🔒 **PR #{pr} 已打开，但 Orbi 读不到它的 check 结果。**",
    ),
    "check_permission.reason": (
        "GitHub answers `Resource not accessible by integration` (HTTP 403) "
        "because this App installation has not approved the `{permission}` "
        "permission (Commit statuses, read-only) the Orbi App now requests. "
        "Orbi re-tries that read every tick and resumes this delivery by "
        "itself as soon as it succeeds — no relabel needed.",
        "GitHub 返回 `Resource not accessible by integration`（HTTP 403）："
        "这个 App 安装尚未批准 Orbi App 现在申请的 `{permission}` 权限"
        "（Commit statuses，只读）。Orbi 每个 tick 都会重试这次读取，一旦成功"
        "就自动恢复这次交付 —— 不需要改标签。",
    ),
    "check_permission.approve": (
        "**Approve it here:** {url}",
        "**在这里批准：** {url}",
    ),
}

# The clarification proposal's line grammar (Issue #1926). The item
# number, the dependency phrase, the acceptance prefix and the criteria
# item are rendered from these templates AND parsed back from them, so a
# translated line grammar and the reader of a translated proposal cannot
# drift apart. The last entry is the one sentence a rendered section adds.
CLARIFY_SUGGESTION_MESSAGES: dict[str, tuple[str, str]] = {
    "clarify.suggestion.item": ("{number}. {result}", "{number}. {result}"),
    "clarify.suggestion.after": ("(after {number})", "（等第 {number} 条）"),
    "clarify.suggestion.acceptance": (
        "   Acceptance: {text}", "   验收：{text}",
    ),
    "clarify.suggestion.criteria_item": ("- {text}", "- {text}"),
    "clarify.suggestion.edit": (
        "Editing this comment changes the proposal.",
        "编辑这条评论即可修改这份提案。",
    ),
    # A split proposal is also an offer (Issue #1927): the section names the
    # command that turns the SAME lines into child tickets, so the reader
    # never has to copy them into new Issues by hand.
    "clarify.suggestion.split_reply": (
        "Reply `/orbi split` to create these tickets.",
        "回复 `/orbi split` 即可创建这些工单。",
    ),
}

# The `/orbi split` command (Issue #1927): its rejections, its receipt and
# the parent references a child ticket carries. Every entry is display text;
# the machine contract (the marker, the labels, the native relations) stays
# in English and outside the catalog.
SPLIT_MESSAGES: dict[str, tuple[str, str]] = {
    "split.reason_no_suggestion": (
        "the newest stop comment carries no split proposal; edit that "
        "comment into a `kind=split` section, then reply again",
        "最新一条停止评论里没有拆分提案；把那条评论改成 `kind=split` 段落，"
        "然后再回复一次",
    ),
    "split.reason_invalid_line": (
        "the split proposal on the newest stop comment is invalid at line "
        "{line}; fix that line and reply again",
        "最新一条停止评论里的拆分提案在第 {line} 行无效；修好那一行后再回复一次",
    ),
    "split.reason_not_split": (
        "the newest stop comment proposes `{kind}`, not a split; "
        "`/orbi split` only turns a `kind=split` proposal into tickets",
        "最新一条停止评论的提案是 `{kind}`，不是拆分；`/orbi split` 只把 "
        "`kind=split` 提案变成工单",
    ),
    "split.reason_child": (
        "this ticket is already a native sub-issue of #{parent}; "
        "a child ticket is not split again",
        "这张工单已经是 #{parent} 的原生子工单；子工单不再拆一次",
    ),
    # The receipt LISTS what exists (`docs/public-contracts.mdx`): a count
    # alone would leave the reader searching GitHub for its own children.
    "split.reason_duplicate": (
        "{count} issues carry the marker `{marker}`: {numbers}; refusing "
        "to adopt any of them (close the extra one and reply again)",
        "有 {count} 张工单都带着标记 `{marker}`：{numbers}；不会擅自采纳其中"
        "任何一张（关掉多余的那张再回复一次）",
    ),
    "split.reason_differs": (
        "issue #{number} carries the marker `{marker}` but its {field} "
        "differs from item {item} of the proposal; nothing more was "
        "created — close or correct that issue and reply again",
        "工单 #{number} 带有标记 `{marker}`，但它的{field}与提案第 {item} 条"
        "不一致；没有继续创建任何工单 —— 关掉或改正那张工单后再回复一次",
    ),
    "split.receipt_title": (
        "**Split into {count} child tickets.**",
        "**已拆分为 {count} 张子工单。**",
    ),
    "split.receipt_child": ("- #{number} {title}", "- #{number} {title}"),
    "split.receipt_parent": (
        "This ticket is now their parent (`ai-epic`) and every child is "
        "queued.",
        "这张工单现在是它们的父工单（`ai-epic`），每张子工单都已进入队列。",
    ),
}

# The one map `language.MESSAGES` merges (the reading point stays
# `language.py`; this module only keeps the entries).
EXTRA_MESSAGES: dict[str, tuple[str, str]] = {
    **DISPLAY_TERMS, **BASE_CI_RED_MESSAGES, **CHECK_PERMISSION_MESSAGES,
    **CLARIFY_SUGGESTION_MESSAGES, **SPLIT_MESSAGES,
    # The rejection a NO-ARGUMENT in-ticket command owes its author
    # (Issue #1922): the shared "write exactly one {label}" wording would
    # contradict the usage line printed beside it for an arity-0 command
    # (`/orbi fix-base now`). It lives here so the catalog owner stays
    # under the size ratchet's cap, like the three maps above.
    "command.reason_malformed_no_argument": (
        "the command line is malformed: `{usage}` takes no argument — "
        "write it on a line of its own",
        "命令行格式不正确：`{usage}` 不接受参数，请单独一行只写它",
    ),
    # The rejection a command owes a caller that cannot supply the context
    # only its own flow knows (Issue #1939): the ticket scan dispatches the
    # whole registry, so the command reaches tickets its flow never opened.
    "command.reason_context_unavailable": (
        "the \u0060{command}\u0060 command needs context this ticket does not "
        "carry ({context}); only the flow that opened the command's own "
        "ticket supplies it, so nothing was changed",
        "\u0060{command}\u0060 命令需要此工单未携带的上下文（{context}）；"
        "只有打开该命令自身工单的流程才会提供它，因此本次未做任何改动",
    ),
    # A receipt's origin for the terminal spelling (Issue #1930): the
    # command came from the host CLI, not from a comment.
    "command.cli_caller": (
        "the `orbi` CLI on the host (login @{login})",
        "主机上的 `orbi` CLI（登录 @{login}）",
    ),
}
