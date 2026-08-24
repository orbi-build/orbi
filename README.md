# Muyan Pilot

本地持续软件开发 Worker：GitHub Issue 是任务池和交付记录。每个 tick 按 TOML
配置中的 source repos 顺序领取一个 `ai-ready` Issue，交给 Pi 在隔离 worktree
中完成 plan → implement → test → verify → PR。成功后 Issue 标记
`ai-pr-opened`，失败标记 `ai-blocked`。

```text
GitHub Issue（ai-ready）
→ Muyan Pilot 每个 tick 领取一个 Issue
→ Pi：plan → implement → test → verify → PR
→ 成功：ai-pr-opened / 失败：ai-blocked
```

## 当前 MVP 范围

- GitHub Issue 与标签是唯一状态存储，不引入数据库、队列、Web UI 或第二个任务系统。
- Runner 是一次性命令：每个 tick 处理一个 Issue 后退出，调度由 systemd user timer 负责；Python 内不实现 daemon loop。
- 不自动 merge，不 push `main`/`master` 等保护分支，交付一律走 PR。
- 没有人为的任务时长上限；命令错误立即失败（fail-fast）并留下日志。
- 没有风险模型、policy engine、多 Agent 并行或自动发布。

```text
bootstrap_runner.py     # 一次性 tick Runner
muyan_pilot.py          # 手工派活（add）与查看队列（status）CLI
prompt.md               # Pi 系统提示词模板
systemd/                # systemd user service + timer
.muyan-pilot.example.toml  # 配置示例（真实 muyan-pilot.toml 不提交）
tests/                  # pytest 测试（100% line/branch coverage）
```

## 前置条件

- `gh` 已登录且有 `repo` 权限：`gh auth status`
- `pi` 可用：`pi --version`（本机为 `/home/xqianliu/.npm-global/bin/pi`）
- Python 3.11+（Runner 只用标准库 `tomllib`，无第三方运行时依赖）
- 一个存放所有业务 repo 的 workspace 目录，例如 `/home/xqianliu/Documents/muyan`

## TOML 配置

配置由人维护，AI 需要修改时通过 PR 修改。仓库只提交示例，真实配置
`muyan-pilot.toml` 被 `.gitignore` 忽略：

```bash
cp .muyan-pilot.example.toml muyan-pilot.toml
# 编辑 muyan-pilot.toml
```

| 键 | 默认值 | 说明 |
|---|---|---|
| `source_repos` | 必填 | 任务源 repo 列表（`"owner/repo"` 格式），按顺序扫描，每个 tick 只领取第一个 `ai-ready` Issue |
| `repo_dir` | `"."` | 创建 feature branch 和 worktree 的 git 仓库（即 muyan-pilot 自身） |
| `workspace_root` | `".."` | Pi 读取、决定修改哪个 repo 的工作区根目录 |
| `prompt` | `"prompt.md"` | Pi 系统提示词模板 |
| `skills` | `[]` | 传给 Pi 的 `--skill` 文件路径列表 |
| `context_files` | `[]` | 注入提示词、要求 Pi 先读的 Markdown 上下文文件路径列表 |

相对路径基于 TOML 文件所在目录解析；路径支持 `~` 和 `$VAR` 展开。
`source_repos` 缺失、为空或含空字符串时启动即报错；`prompt`、`skills`、
`context_files` 指向的文件不存在时同样立即报错。

默认配置路径是 `muyan-pilot.toml`，可用环境变量覆盖（systemd unit 即用它
指向绝对路径）：

```bash
MUYAN_PILOT_CONFIG=/path/to/muyan-pilot.toml /usr/bin/python3 bootstrap_runner.py
```

## 两个 source repo 的配置

`source_repos` 的顺序就是扫描顺序，每个 tick 只领取一个 Issue。典型配置是
先 Pilot 自己的开发任务，再业务任务池：

```toml
source_repos = [
  "xqliu/muyan-pilot",
  "xqliu/muyan-ceo",
]
```

- 第一个 repo 有待办时，第二个 repo 不会被领取；
- 手工派活到指定 source repo 时，repo 必须在 `source_repos` 中（见下文
  `add --repo`）；
- 这是两个固定 repo 的顺序扫描，不是通用多租户队列。

## systemd user service/timer

夜间窗口 01:00–06:55 内每 15 分钟自动执行一次（触发点 01:00、01:15、…、
06:45）。安装：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/muyan-pilot.service systemd/muyan-pilot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now muyan-pilot.timer
systemctl --user list-timers muyan-pilot.timer
```

调度行为（见 `systemd/muyan-pilot.timer`）：

- `OnCalendar=*-*-* 01..06:00/15:00`：夜间窗口 01:00–06:55，空闲轮询间隔 15 分钟；
- `Persistent=false`：错过的 tick 直接丢弃，不排队补跑；
- service 正在运行时，systemd 不会启动第二个实例；
- `AccuracySec=30s`：触发时间允许 30 秒漂移。

service 本身（见 `systemd/muyan-pilot.service`）：

- `WorkingDirectory` 和 `MUYAN_PILOT_CONFIG` 指向
  `%h/Documents/muyan/muyan-pilot` 下的真实配置；
- `TimeoutStartSec=infinity` 只是部署适配（关闭 systemd 默认启动 kill 限制），
  不是任务 timeout；unit 中没有 `RuntimeMaxSec`/`TimeoutStopSec`，
  任务时长不受限；
- 日志输出到 journal（`StandardOutput=journal`）。

## 手工 single-tick 调试

手工执行一个 tick 只用于首次验证或排查，不是日常调度方式：

```bash
/usr/bin/python3 bootstrap_runner.py --config muyan-pilot.toml
```

行为：

- 没有 `ai-ready` Issue：打印 `outcome=no_ready_issue` 并以 0 退出；
- 有待办：领取一个 Issue → 加 `ai-in-progress` → 启动 Pi → 校验 PR →
  成功加 `ai-pr-opened` 并在 Issue 评论 PR 链接，失败加 `ai-blocked` 并
  评论失败原因；
- 任何外部命令失败立即抛错退出，不做回退。

## 任务派发与状态

`muyan_pilot.py` 是最小 CLI，用于手工派活和查看队列：

```bash
# 在第一个配置的 source repo 创建 Issue 并自动添加 ai-ready
python3 muyan_pilot.py add "任务标题" --body "任务描述" --config muyan-pilot.toml

# 派发到指定 source repo（必须在配置 source_repos 中）
python3 muyan_pilot.py add "任务标题" --repo xqliu/muyan-ceo --config muyan-pilot.toml

# 查看每个 source repo 的当前任务（ai-in-progress）、待办（ai-ready）和最近结果（ai-pr-opened / ai-blocked）
python3 muyan_pilot.py status --config muyan-pilot.toml
```

`add` 成功后打印新 Issue 的 URL 和 `ai-ready` 标签；`status` 只读，不修改
任何标签。命令失败立即报错，不做回退。

## Pi skills/context 配置

`skills` 是传给 Pi 的 `--skill` 文件路径，`context_files` 是注入提示词、
要求 Pi 开工前先读的 Markdown 文件路径。两者都支持绝对路径、`~`、`$VAR`
和相对 TOML 文件的路径。本机参考配置：

```toml
skills = [
  "~/.agents/skills/tdd-dev/SKILL.md",
  "~/Documents/agent-skills/skills/code-review/SKILL.md",
  "~/Documents/agent-skills/skills/review-fix-loop/SKILL.md",
]

context_files = [
  "../muyan-ceo/MUYAN-PILOT-CONTEXT.md",
]
```

`prompt.md` 使用 `{{SOURCE_REPO}}`、`{{SOURCE_REPOS}}`、`{{ISSUE_NUMBER}}`、
`{{ISSUE_TITLE}}`、`{{ISSUE_BODY}}`、`{{WORKSPACE_ROOT}}`、`{{CONTEXT_FILES}}`
和 `{{SKILLS}}` 占位符，由 Runner 在启动 Pi 前替换。

## 测试与 100% coverage

仓库要求 Python 代码 100% line + branch coverage。开发依赖只有
`pytest` 和 `pytest-cov`（`--cov` 参数由 pytest-cov 提供；运行时零第三方
依赖）：

```bash
/usr/bin/python3 -m pip install pytest pytest-cov
/usr/bin/python3 -m pytest --cov=bootstrap_runner --cov=muyan_pilot --cov-branch -q
```

输出中 `bootstrap_runner.py` 和 `muyan_pilot.py` 两行都必须是
`100%` 才算通过。

## 排查方法

标签是现场的第一线索：`ai-in-progress` 正在执行，`ai-blocked` 需要人工
处理（Issue 评论里有失败原因），`ai-pr-opened` 已交付 PR。

1. journal（Runner 和 Pi 的 stdout/stderr 都在这里）：

   ```bash
   journalctl --user -u muyan-pilot.service -n 200 --no-pager
   journalctl --user -u muyan-pilot.service -f   # 实时跟踪
   ```

2. Pi session（每次任务一个目录，含 Pi 的会话记录）：

   ```bash
   ls -lt /tmp/muyan-pilot-*-issue-*/.pi-session
   ```

3. worktree（Pi 的工作目录，含 feature branch 和未清理的现场）：

   ```bash
   git -C /home/xqianliu/Documents/muyan/muyan-pilot worktree list
   # 任务分支：muyan-pilot/<owner>-<repo>-issue-<n>
   # worktree 目录：/tmp/muyan-pilot-<owner>-<repo>-issue-<n>
   ```

4. 任务卡死（没有命令错误、只是不动）：通过 journal 确认状态后人工停止，
   再处理 Issue 标签：

   ```bash
   systemctl --user stop muyan-pilot.service
   gh issue edit <n> --repo <owner/repo> --remove-label ai-in-progress --add-label ai-blocked
   ```

## 行为边界（fail-fast / 不自动 merge）

- 任何外部命令（`gh`、`git`、`pi`）失败立即抛错退出并记录日志，没有重试、
  回退或静默降级；
- 成功判定严格：Pi 结束后必须仍在任务分支上，且该分支有且只有一个 open
  PR，否则按失败处理；
- 不自动 merge；不 push `main`/`master` 等保护分支；PR 由人工审核合并；
- 没有人为的任务时长上限；命令错误立即失败，真正卡死时通过
  systemd/journal 排查并人工停止；
- 15 分钟只是空闲时的轮询间隔，不是任务 timeout。
