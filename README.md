# Muyan Pilot

最小 bootstrap：从配置文件中的 source repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR。

## 当前运行

```bash
/usr/bin/python3 bootstrap_runner.py --config muyan-pilot.toml
```

正常运行使用 systemd user timer，每 15 分钟自动执行一次：

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

## 任务 worktree

每个任务的现场放在配置 repo 的 `.worktrees/` 目录下，目录名包含 source repo 与 Issue 编号（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14`），任务完成后随项目保留，便于回看现场。`.worktrees/` 已加入 `.gitignore`，不会进入版本库。
