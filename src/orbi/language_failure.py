"""The failure vocabulary of Orbi's comments and progress scenes.

Split out of `language.MESSAGES` the same way `language_terms` was (Issue
#1729): the catalog owner stays inside the size ratchet, so a failure report
sentence can be added without growing `language.py`. Every key here is the
human prose of a delivery failure — the blocked/fix-needed headlines, the run
scene field labels, the evidence headings, the recovery and repair paths and
the one automatic re-queue sentence (Issue #2202). No machine row (run marker,
`run_id=`, `orbi:failure:v1`) has an entry here.

`language.MESSAGES` merges this map in place, so `language.text()` remains the
ONE reading point and no caller imports this module directly.
"""
from __future__ import annotations

FAILURE_MESSAGES: dict[str, tuple[str, str]] = {
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
    # Issue #1749, #1753: the configured base branch does not exist in
    # the repository. The reason names the CONFIGURED branch (Git's own
    # line only carries the value it was asked to fetch) and says what is
    # wrong; the action names both repairs — create the branch, or pick an
    # existing one where this repository was set up (the web connection or
    # the config file, so a hosted tenant has a place to go too) — and how
    # to start the Issue over. Git's raw answer stays in the run details.
    # Issue #2222: a failed release names the STEP it failed at, so the
    # reader knows whether a tag, the version commit or a gate went wrong.
    # `step` is a machine value from the release state machine (tag,
    # version, gates, ...) and stays verbatim; only the sentence moves.
    "failure.release_failed_reason": (
        "The release failed at the {step} step: {detail}",
        "release 在 {step} 步骤失败：{detail}",
    ),
    "failure.base_branch_missing_reason": (
        "This repository has no branch \u0060{base_branch}\u0060, so Orbi "
        "doesn't know which branch to start from.",
        "这个仓库里没有分支 \u0060{base_branch}\u0060，Orbi 不知道该从哪个"
        "分支开始改。",
    ),
    "failure.terminal_base_branch_missing": (
        "Either create \u0060{base_branch}\u0060 in the repository, or switch "
        "Orbi to a branch that exists, wherever you set up this repository "
        "(the branch you picked when connecting it on the web, or "
        "base_branch in your config file). Then remove \u0060ai-blocked\u0060 "
        "and add \u0060ai-ready\u0060 again, and Orbi will start over.",
        "两种改法选一种：在仓库里建好分支 \u0060{base_branch}\u0060；或者把 "
        "Orbi 用的基础分支换成仓库里已有的分支，在你当初设置这个仓库的地方"
        "改（网页上连接仓库时选的分支，或配置文件里的 base_branch）。改好后"
        "去掉 \u0060ai-blocked\u0060 标签、重新加上 \u0060ai-ready\u0060，"
        "Orbi 会重新开始。",
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
        "permission: {unblock_step} to continue PR {pr_url}. If no update "
        "is available or the same error persists, contact the App "
        "operator.",
        "{cause}。请检查该 App 安装设置里是否可授予包含 Workflows 读写权限的"
        "权限更新。权限解决后：{unblock_step}，继续 PR {pr_url}。如果无法"
        "更新权限或同样的错误仍然出现，请联系 App 的运维者。",
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
        "— restore the trusted 'Orbi opened PR' scene comment, then "
        "{unblock_step} to resume this same PR",
        "Orbi 失败：{error}；这是 AI 无法安全判断或修复的外部前提条件，"
        "因此无法自动恢复（Issue 会保持 ai-blocked，直到人工决定）——"
        "请恢复可信的 'Orbi opened PR' 场景评论，然后{unblock_step}，"
        "以继续同一个 PR",
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
    "failure.milestone_blocked": (
        "blocked: {summary}", "已阻塞：{summary}",
    ),
    "failure.milestone_fix_needed": (
        "fix needed: {summary}", "需要修复：{summary}",
    ),

    "failure.next_step_blocked_classified": (
        "fix the precondition above (see the reason): {unblock_step} to "
        "resume this same PR",
        "修复上面的前提条件（见原因）：{unblock_step}，以继续同一个 PR",
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
    "failure.requeued": (
        "Orbi re-queued this Issue automatically: the delivery run hit a "
        "temporary GitHub error before any PR existed ({reason}), so it was "
        "put back in the queue. The next tick claims it again as a new run "
        "— no action needed.",
        "Orbi 已自动重新排队这个 Issue：本次交付在任何 PR 存在之前遇到临时的 "
        "GitHub 错误（{reason}），因此已放回队列。下一个 tick 会把它作为一次"
        "新的运行重新领取——无需任何操作。",
    ),
}
