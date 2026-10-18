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
