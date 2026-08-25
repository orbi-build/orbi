# Muyan Pilot

最小 bootstrap：从配置文件中的 source repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR；随后 Runner 自动完成独立审查、修复循环和合并（见下方「自动审查、修复与合并」）。

开发契约见 [AGENTS.md](AGENTS.md)：每次本地 Pi 自举开发前先读 Issue、context files、README 和相关代码，TDD、100% 覆盖率、UI 用 Playwright、失败 fail fast、不 merge、不 push 保护分支、不引入数据库/队列/daemon/fallback、不设业务任务 timeout。

## 当前运行

```bash
/usr/bin/python3 bootstrap_runner.py --config muyan-pilot.toml
```

正常运行使用 systemd user timer，全天 24 小时运行，每 15 分钟自动执行一次（触发点覆盖 00:00–23:45）：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/muyan-pilot.service systemd/muyan-pilot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now muyan-pilot.timer
systemctl --user list-timers muyan-pilot.timer
```

手工命令只用于首次验证或立即执行一个 tick，不是日常调度方式。

## 任务派发与状态

`muyan_pilot.py` 是最小 CLI，用于手工派活和查看队列。GitHub Issue 与标签是唯一状态存储，不引入数据库或 Web UI：

```bash
# 在第一个配置的 source repo 创建 Issue 并自动添加 ai-ready
python3 muyan_pilot.py add "任务标题" --body "任务描述" --config muyan-pilot.toml

# 派发到指定 source repo（必须在配置 source_repos 中）
python3 muyan_pilot.py add "任务标题" --repo xqliu/muyan-ceo --config muyan-pilot.toml

# 查看每个 source repo 的当前任务（ai-in-progress）、待办（ai-ready）和最近结果（ai-pr-opened / ai-blocked）
python3 muyan_pilot.py status --config muyan-pilot.toml
```

`add` 成功后打印新 Issue 的 URL 和 `ai-ready` 标签；`status` 只读，不修改任何标签。命令失败立即报错，不做回退。

前置条件：

- `gh auth status` 成功；
- `pi --version` 成功；
- 配置文件存在且包含 `source_repos`；
- 配置中的 repo、workspace、prompt 和 context 路径正确。

配置使用 TOML，由人维护，AI 通过 PR 修改。开源仓库只提交 example，真实配置不提交：

```toml
cp .muyan-pilot.example.toml muyan-pilot.toml
# 编辑 muyan-pilot.toml
```

Runner 每次处理一个 Issue 后退出，由 systemd timer 再次触发；不在 Python 内实现 daemon，不引入数据库、队列、重试或复杂恢复。没有人为的任务时长上限；命令错误立即失败，真正卡死时通过 systemd/journal 排查并人工停止。

## 任务 base 与 worktree

每次领取任务前，Runner 在配置 repo 中执行 `git fetch origin <base_branch>`，并冻结 `origin/<base_branch>` 的精确 SHA（`base_branch` 在 TOML 中配置，默认 `main`）。任务 worktree 和 feature branch 都从该 SHA 创建，绝不使用主工作区当前 HEAD；branch 和目录名都带唯一 run 标识（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14-a1b2c3d4`），同一个 Issue 返工时会生成新的独立 run，旧现场原样保留。base branch、base SHA 和 run 标识会写入 Issue 评论和 `status` 输出。

Pi 创建 PR 前必须重新 fetch：若 `origin/<base_branch>` 已前进，需合入最新 base、手工解决冲突、重跑完整测试与 review 后再推送。Runner 在验收时用 `git merge-base --is-ancestor origin/<base_branch> HEAD` 验证最新远端 base 是交付 HEAD 的祖先；不满足则 fail fast，不接受 PR。不自动解决冲突，不 force push，不 merge 或 push 保护分支。

`.worktrees/` 已加入 `.gitignore`，不会进入版本库。

## 自动审查、修复与合并

Pi 不直接 push 保护分支。实现 Agent 只 push feature branch 并创建 PR；PR 打开后由 **Runner** 关闭交付闭环：

1. **冻结 PR 的 base/head SHA**（`gh pr list` 取唯一 open PR 的 `baseRefOid`/`headRefOid`）。
2. **独立审查**：启动一个独立、只读的 Review Agent（code-review R1–R9），对精确 base/head SHA 审查需求、diff、调用链、测试与运行证据。审查会话必须以一行机器可读的 `REVIEW_VERDICT {"verdict":"pass|findings","blockers":N,"majors":N,"minors":N,"findings":[...]}` 结尾；读不到合法 verdict 一律 fail fast，绝不当作通过。
3. **修复循环**：有 Blocker/Major 时，把 finding 评论到 Issue 和 PR，在同一 feature branch/worktree 启动 Fixer 修复并 push，然后重新冻结 SHA、全量回归、完整复审。循环最多 5 轮（见 `MAX_REVIEW_ROUNDS`）；超轮仍有 Blocker/Major 则 fail fast 并标记 `ai-blocked`。
4. **合并门禁**：重新 fetch 最新 `origin/<base>`，要求 PR head 包含最新 base、PR mergeable、远端 head 仍是被审查的 head；然后 `gh pr merge <n> --match-head-commit <head> --merge`，只有被审查的 head 能落地。落后最新 base 的 PR 不会被合并（fail fast，等下一 tick 吸收最新 base 后重试）。
5. **确认合并**：`gh pr view` 确认 PR `MERGED` 且 `mergeCommit` 已落在 `origin/<base>`；Issue 因 PR 关联自动 CLOSED。

成功合并后 Issue 标记 `ai-merged`（替代 `ai-pr-opened`），评论写入 PR URL、merge commit、审查轮次和 base/run 信息。下一任务只从新的 `origin/<base>` 创建。不 force push、不直接 push 保护分支、不设业务 timeout；审查 finding 不是 `ai-blocked`，而是进入同一 PR 的 fix/review 循环。

三个 prompt 由配置提供（默认 `prompt.md` 实现、`prompt_review.md` 审查、`prompt_fix.md` 修复）。
