# Muyan Pilot

最小 bootstrap：从配置文件中的 source repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR。

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

## 自动可观测（正常运行不需要执行任何命令）

正常运行完全自动化：人不需要执行 status 命令、不需要轮询进程、不需要督工。
任务进入 GitHub Issue 池后，Runner 自己完成领取 → plan → implement → test →
verify → PR，并主动发布过程和最终结果。`muyan_pilot.py status` 只保留为
开发/故障排查附件，不是产品入口，也不能作为自动可观测性的验收证据。

### journal（本地，systemd）

`bootstrap_runner.py` 运行 Pi 期间每 15 秒读取任务 worktree 里的 Pi session
JSONL（`.pi-session/*.jsonl`），把最近活动写入 journal（systemd 日志）；已经
打开 `journalctl -f` 时内容持续自动刷新：

- `pi_heartbeat issue=... run_id=... role=... source_repo=... branch=... worktree=... session=... session_file=... phase=... last_activity=... last=... elapsed=...`——心跳间隔不超过 30 秒（15 秒轮询 + 30 秒阈值保证），即使 session 安静也持续有行；
- `pi_event ...`——session 出现新事件（阶段/动作变化）时立即记录；阶段包括 test / pr / push / commit / base / worktree / ui / bash 或工具名，last 是脱敏后的工具/命令摘要；
- `pi_idle ... stale_seconds=...`——超过 5 分钟没有模型/session 活动时告警一次，带完整现场（找不到 session 文件时同样告警）；
- `pi_resumed ...`——idle 告警后 session 恢复活动时立即记录；
- `pi_failed returncode=... ...`——进程异常退出时先记录现场再抛出错误；session JSONL 完整保留在 worktree 中，作为本地完整记录。

每行都带 issue、run id、role（implement / review / fix / merge）、phase、
elapsed、last activity、last action、session、branch。implementer、reviewer、
fixer 三种 Pi session 用同一个机制观测，role 由 Runner 在启动 session 时传入。

### GitHub（手机，自动更新）

领取任务后，Runner 在 source Issue 上创建一条带隐藏 run marker
（`<!-- muyan-pilot:run=<run_id> -->`）的进度评论，之后只 PATCH 同一条
评论（每 30 秒或进度变化时），不新增 heartbeat 垃圾评论。评论始终显示：
当前阶段、role、已运行时间、最近活动时间、最近动作、测试状态、review/fix
round、branch、PR/merge 状态。进程重启后按 run marker 找回同一条评论继续
更新，不需要数据库。

关键 milestone 单独发布简短评论（`Muyan Pilot: ...`），让 GitHub Mobile
主动推送通知：started、plan ready、tests passed/failed、review findings、
fix pushed、PR opened、merged、blocked。完成后进度评论更新为最终交付摘要
（PR、测试、审查证据）；真正失败时更新为 blocked 现场和下一步原因，同时
Issue 标记 `ai-blocked`。

### 调试附件

`muyan_pilot.py status` 只读展示当前（`ai-in-progress`）任务的实时状态（仅供开发/故障排查）：

```bash
python3 muyan_pilot.py status --config muyan-pilot.toml
# source: xqliu/muyan-pilot
#   base: main abc123def456
#   current: #24 Stream live Pi activity ... https://github.com/xqliu/muyan-pilot/issues/24
#     live: phase=test last_activity=2026-08-25T02:30:00Z last=bash pytest tests/
#     session: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>/.pi-session/<session>.jsonl
#     worktree: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>
#   ready: -
#   result: -
```

journal、`status` 和 GitHub 进度评论只暴露脱敏摘要：完整 prompt、Issue body 和 token 不会写入日志或评论（命令日志固定为 `<redacted>`，工具摘要截断到 200 字符并屏蔽常见 token 形状）。

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
