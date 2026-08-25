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

## 实时进展

Pi 长时间运行时，Runner 不再只留下启动命令和最终结果。`bootstrap_runner.py` 运行 Pi 期间每 15 秒读取任务 worktree 里的 Pi session JSONL（`.pi-session/*.jsonl`），把最近活动写入 journal（systemd 日志）：

- `pi_activity issue=... source_repo=... branch=... worktree=... session=... session_file=... events=... phase=... last_activity=... last=...`——有新事件时记录当前阶段（test / pr / push / commit / base / worktree / ui / bash 或工具名）、最近活动时间和脱敏后的工具/命令摘要；
- `pi_idle ... stale_seconds=...`——超过 5 分钟没有新事件时告警，带完整现场（找不到 session 文件时同样告警）；
- `pi_failed returncode=... ...`——进程异常退出时先记录现场再抛出错误；session JSONL 完整保留在 worktree 中，作为本地完整记录。

`muyan_pilot.py status` 同时展示当前（`ai-in-progress`）任务的实时状态：

```bash
python3 muyan_pilot.py status --config muyan-pilot.toml
# capacity: 1
# slots: 1/1
#   slot-1: pid=4321
# source: xqliu/muyan-pilot
#   base: main abc123def456
#   current: #24 Stream live Pi activity ... https://github.com/xqliu/muyan-pilot/issues/24
#     live: phase=test last_activity=2026-08-25T02:30:00Z last=bash pytest tests/
#     session: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>/.pi-session/<session>.jsonl
#     worktree: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>
#   ready: -
#   result: -
```

顶部的 `capacity` / `slots` 是当前机器的并发容量（`max_concurrency`）与已占用 slot（含持有者 PID），见下一节。

journal 和 `status` 只暴露脱敏摘要：完整 prompt、Issue body 和 token 不会写入日志（命令日志固定为 `<redacted>`，工具摘要截断到 200 字符并屏蔽常见 token 形状）。关键阶段继续回写 GitHub Issue 评论：Pi 启动（含 branch 和 worktree）、PR 创建、失败现场。

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

Runner 每次处理一个 Issue 后退出，由 systemd timer 再次触发；不在 Python 内实现 daemon，不引入数据库、队列、重试或复杂恢复。没有人为的任务时长上限；命令错误立即失败，真正卡死时通过 systemd/journal 排查并人工停止。并发上限见下一节 `max_concurrency`：拿不到 slot 的 Runner 记录 `capacity_full` 后正常退出，不领取 Issue。

## 并发限制（max_concurrency）

本机允许的 Pilot 并发任务数由 `muyan-pilot.toml` 的 `max_concurrency` 配置：必须是正整数，缺失时默认 1（本地 AI/GPU 只能稳定服务一个任务）；非整数、布尔值、0 或负数启动即 fail fast。slot 状态在 `<repo_dir>/.muyan-pilot/slots/slot-N`（N = 1..max_concurrency），每个 slot 文件由 `O_EXCL` 原子创建、内容写持有者 PID——跨进程互斥，不依赖进程内计数或 GitHub `ai-in-progress` 标签，多个 systemd/manual Runner 同时启动也不会突破限制。

- 并发额度按完整任务生命周期计算：Runner 在领取 Issue 之前取得 slot，implement → review → fix → PR 期间始终占用，进程退出时释放；
- 同一任务内部 implement/review/fix 在同一个 Pi session 内串行执行，共用同一个 slot，任意时刻最多一个 Pi 子进程；
- 达到 `max_concurrency` 时，新 Runner 不领取 Issue、不修改标签、不调用 Pi，记录结构化日志 `capacity_full max_concurrency=... slot_dir=...` 后正常退出（退出码 0），等 systemd timer 下次触发；
- slot 在进程正常结束（atexit）和 SIGTERM/SIGINT（systemd stop / Ctrl+C）时自动删除；被 SIGKILL 的进程无法运行清理，但其 slot 文件的 PID 已不在运行，下一个 Runner 会把它当作 stale slot 重新领取——异常退出不会造成永久锁死；
- `muyan_pilot.py status` 显示配置容量和当前已占用 slot（`capacity: N`、`slots: k/N`、`slot-N: pid=...`）；
- 直接在 Pilot 外手工运行的任意 `pi` 命令不属于该配置控制范围：`max_concurrency` 只约束 Runner 领取任务时启动的 Pi，手工 `pi` 不受 slot 管理，也不会释放或占用任何 slot。

## 全链路 run_id（correlation ID）

每个任务 attempt 只生成一次 `run_id`（8 位 hex，例如 `e07383c2`），语义等同 trace ID：implement、review、fix、merge 全部复用同一个值；同一个 Issue retry 时生成新的 run_id，Issue number 是多个 run 的共同父标识。不创建 `trace_id`/`log_id`/另一套 UUID，不引入 tracing backend。

同一个 run_id 出现在：

- 该 attempt 的每条 journal 日志首字段：`[e07383c2] command=...`；
- start / PR opened / failed 等 Issue 评论：可见字段 `run_id=e07383c2` + 隐藏 marker `<!-- muyan-pilot:run=e07383c2 -->`；
- feature branch 与 worktree 名（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14-a1b2c3d4`）；
- Pi session 目录（worktree 内 `.pi-session/`）与 plan/test/verify/review 等 run artifacts 的路径；
- 注入 Pi 的 prompt context（`Run id: ...`）；
- PR body 的稳定 machine-readable marker `<!-- muyan-pilot:run=e07383c2 -->`——Runner 验收时校验，缺失即 fail fast，拒绝该 PR；
- Pi 自己发出的 progress / review / fix / 最终评论（prompt 要求携带同一 marker 和 `run_id=` 字段）。

查询方式（不依赖内存映射，进程重启后仍可恢复关联）：

```bash
# journal 中还原一个 run 的完整时间线
journalctl --user -u muyan-pilot.service | grep e07383c2

# GitHub 上搜索一个 run 的 progress / milestone / review / merge 记录
gh search issues "e07383c2" --repo xqliu/muyan-pilot

# 本地在 repo 中搜索 run_id 找到 worktree、session 和 run artifacts
grep -r e07383c2 /home/xqianliu/Documents/muyan/muyan-pilot/.worktrees/
```

缺少合法 run_id 的 run-scoped 事件（绑定 run、构建 GitHub marker、PR body 校验）会 fail fast，不做回退。

## 任务 base 与 worktree

每次领取任务前，Runner 在配置 repo 中执行 `git fetch origin <base_branch>`，并冻结 `origin/<base_branch>` 的精确 SHA（`base_branch` 在 TOML 中配置，默认 `main`）。任务 worktree 和 feature branch 都从该 SHA 创建，绝不使用主工作区当前 HEAD；branch 和目录名都带唯一 run 标识（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14-a1b2c3d4`），同一个 Issue 返工时会生成新的独立 run，旧现场原样保留。base branch、base SHA 和 run 标识会写入 Issue 评论和 `status` 输出。

Pi 创建 PR 前必须重新 fetch：若 `origin/<base_branch>` 已前进，需合入最新 base、手工解决冲突、重跑完整测试与 review 后再推送。Runner 在验收时用 `git merge-base --is-ancestor origin/<base_branch> HEAD` 验证最新远端 base 是交付 HEAD 的祖先；不满足则 fail fast，不接受 PR。不自动解决冲突，不 force push，不 merge 或 push 保护分支。

`.worktrees/` 已加入 `.gitignore`，不会进入版本库。
