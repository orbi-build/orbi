# Muyan Pilot

最小 bootstrap：从配置文件中的 source repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR；随后 Runner 自动完成独立审查（会话内修复）和合并（见下方「自动审查、修复与合并」）。

开发契约见 [AGENTS.md](AGENTS.md)：每次本地 Pi 自举开发前先读 Issue、context files、README 和相关代码，TDD、100% 覆盖率、UI 用 Playwright、失败 fail fast、不 merge、不 push 保护分支、不引入数据库/队列/daemon/fallback、不设业务任务 timeout。

## 文档站

面向新用户的完整开源文档（安装前提、配置、首次启动、smoke walkthrough、工作流、运维、安全、测试、贡献）在仓库 [`docs/`](docs/) 目录：`docs/docs.json` + `docs/*.mdx`，由 Mintlify 构建和托管（连接默认分支 `main`，Git Settings 的 documentation path 配置为 `/docs`，每次合并后自动构建发布）。Mintlify 默认地址为 <https://muyan-pilot.mintlify.site>（若绑定了自定义域名，以 Mintlify 控制台配置的地址为准）。文档站提供英文（默认）和中文（[`docs/zh/`](docs/zh/)，Mintlify i18n 语言切换）两个语言版本，两种语言共享同一事实源（同一套命令、标签、配置字段和端口，不复制出互相漂移的实现说明）。仓库内的 Markdown/MDX 是唯一事实源，Mintlify 只负责构建、搜索和托管，不产生第二份内容；本 README 保留 GitHub 首页与运行契约概览。

两张总览图（Mintlify 渲染 Mermaid，仓库内保留可读源码）：

- **系统架构总览**（GitHub Issues、systemd timer/service、Runner、Pi、worktree、llama-server、可选 `local-llm-kv-cache` proxy、PR/review/merge）：文档站首页 [index](docs/index.mdx) / [zh/index](docs/zh/index.mdx)；
- **任务生命周期状态机**（`ai-ready` → `ai-in-progress` → `ai-pr-opened` → `ai-fix-needed` → `ai-merged` / `ai-blocked`，标出 Epic/Release task/P0 边界）：[workflow](docs/workflow.mdx) / [zh/workflow](docs/zh/workflow.mdx)。

## 当前运行

```bash
/usr/bin/python3 bootstrap_runner.py --config muyan-pilot.toml
```

正常运行使用 systemd user timer，全天 24 小时运行，每 15 分钟自动执行一次（触发点覆盖 00:00–23:45）：

```bash
# 幂等安装用户级 service/timer（仓库模板复制到用户 systemd 目录、
# daemon-reload、enable timer），并输出部署 commit/hash：
python3 muyan_pilot.py install-units --config muyan-pilot.toml
systemctl --user list-timers muyan-pilot.timer
```

`install-units` 是幂等的：重复执行只会把仓库模板重新复制到位并 `daemon-reload`，**不会**启动、停止或重启正在运行的 Runner（新配置从下一次 service 启动生效）。手工命令只用于首次验证或立即执行一个 tick，不是日常调度方式。

## 代码更新（Issue #52）

service 每次真正启动时，先由 `ExecStartPre` 在 Python Runner 进程外执行：

```bash
git fetch origin main && git merge --ff-only origin/main
```

本地 main 被 fast-forward 到最新 `origin/main` 后，Runner 才用新代码启动。当前正在运行的长任务不会被热更新、不会被杀、也不会启动第二个 Runner（service active 时 systemd 忽略 timer 的 start 请求；下一次 service 真正启动时生效）。main 工作区不干净、fetch 失败或无法 fast-forward 时，preflight 命令失败，service 不启动，原因写入 systemd journal（fail fast）。不新增 refresh service、worker、dispatcher 或常驻进程；15 分钟 timer 配置保持不变。

## 部署一致性（Issue #103）

仓库中的 `systemd/muyan-pilot.service` 和 `systemd/muyan-pilot.timer` 是已安装 unit 的**唯一事实源**：代码和实际运行配置必须一致，漂移必须能被明确发现。

**幂等安装**：`python3 muyan_pilot.py install-units` 把两个模板复制到用户 systemd 目录（`~/.config/systemd/user/`，可用 `--installed-dir` 覆盖）、执行 `systemctl --user daemon-reload`、`systemctl --user enable --now muyan-pilot.timer`，并输出部署 commit（部署 checkout 的 HEAD，即模板来源）和每个 unit 的 sha256。安装**不会**启动、停止或重启 service：当前运行中的 Runner 不被中断，新配置从下一次 service 启动生效。

**启动前漂移检查**：Runner 每次启动时（`ExecStartPre` 同步完 checkout 之后、领取任何 Issue 之前）对比已安装 unit 与仓库模板（service 和 timer 都覆盖）。一致时记录 `unit_drift clean`；发现漂移时记录结构化日志并 fail fast（非零退出，不取 slot、不领取 Issue、不改任何标签），直到 unit 同步：

```text
unit_drift unit=muyan-pilot.timer repo=<repo path> installed=<installed path> repo_sha256=... installed_sha256=... fix=python3 muyan_pilot.py install-units
```

**只读诊断**：`python3 muyan_pilot.py doctor`（可用 `--installed-dir` 指定检查目录）报告 repo commit、unit drift（clean 或具体漂移 + 修复命令）、Git transport（配置的 origin URL、protocol、SSH 探测；见「Git transport」）、timer/service active 状态、Runner slot、Pi session、每个 source repo 的当前 Issue 和最近 journal 活动。只读：不改标签、不改 unit、不做 git 变更。

**完整部署时序**（从代码合并到下一次 Runner 启动）：

```text
git merge 到 main
  -> install units（仓库模板复制到用户 systemd 目录，幂等，不碰运行中的 Runner）
  -> daemon-reload
  -> timer 下一次触发
  -> ExecStartPre 同步 origin/main（fetch + fast-forward）
  -> 启动前 unit 漂移检查（一致才继续）
  -> 启动前 Git transport 检查（SSH 且可达才继续，见下节）
  -> Runner 启动并执行一个 Issue
```

## Git transport（Issue #114）

两条认证通道，职责边界清晰：

- **Git 数据操作**（fetch、push——包括推送 `.github/workflows/*.yml`）走 **SSH**（`git@github.com:owner/repo.git`，用本机 SSH key 认证）。workflow 文件推送不再依赖 OAuth App 的 `workflow` scope（#106 被 HTTPS/OAuth 通道阻塞的根因）；
- **GitHub API 操作**（Issue、PR、label、comment、merge）继续走现有 `gh` token。SSH 从不作为 API 认证，`gh` token 也从不用于 git 数据。

部署 checkout 的单一 `origin` remote 就是 transport：`git worktree add` 创建的任务 worktree 共享主仓库的 remote 配置（已对真实 git 验证），所以 transport 只在 checkout 上配置一次，所有 worktree 天然继承——**新 bootstrap worktree 的 `git remote -v` 默认就是 SSH**。

**启动前检查**：Runner 每次启动时（unit 漂移检查之后、取 slot/领取任何 Issue 之前）校验 checkout 的 transport——**配置的** `origin` URL（`git config remote.origin.url`，不是 insteadOf 重写后的数据面 URL）必须是第一个配置 source repo 的 SSH 形式，且 `git ls-remote <ssh-url>` 退出码 0（SSH 可达且已认证，已对真实 CLI 验证）。失败时记录结构化日志并 fail fast（非零退出，不取 slot、不领取 Issue、不改任何标签），**不自动降级到 HTTPS，也不静默跳过 workflow 文件**：

```text
transport_check_failed repo_dir=<repo path> source_repos=<...> reason=ssh_unreachable: git ls-remote git@github.com:owner/repo.git failed: ... stderr=git@github.com: Permission denied (publickey). — ...
```

**已有 HTTPS remote 的迁移路径**：Runner 从不静默改写 remote，也从不从评论或 Issue 内容读取 remote。迁移只由人工执行的一次性 setup 入口完成（`python3 muyan_pilot.py setup`，内部执行 `git remote set-url origin git@github.com:owner/repo.git`）；其他路径遇到 HTTPS remote 时 fail fast，失败信息携带确切的迁移命令。指向其他仓库的 remote 从不被迁移（改写会把 checkout 指向另一个仓库），无论 setup 是否授权迁移都直接以 mismatch 现场 fail fast。手工等价命令：

```bash
git remote set-url origin git@github.com:OWNER/REPO.git
```

**只读诊断**：`python3 muyan_pilot.py doctor` 报告 transport 行（remote、配置的 URL、protocol、期望 URL、SSH 探测结果）；SSH 不可用时 doctor 把失败现场写进报告（`transport: FAILED ...`），不中断其余报告——fail-fast 门禁是启动前检查，doctor 是诊断报告。

## 远程 CI（GitHub Actions）

仓库契约（全量 pytest + 100% line/branch coverage）不只在本机 Runner 上跑：`.github/workflows/ci.yml` 让 GitHub Actions 在每次 `pull_request` 和每次 `push` 到 `main` 时跑同一份契约，测试失败或覆盖率低于 100% 时 CI 变红。单个 job，不加 lint、矩阵或缓存（Issue #56）。

与本地的差异：生产运行时是 Runner 机器的 `/usr/bin/python3`（3.14.6），GitHub-hosted runner 没有这个解释器，所以 workflow 用 `actions/setup-python` 固定同一 minor 版本 `3.14`，契约命令通过 PATH 上固定的 `python3` 执行（命令与本地相同，只有解释器路径不同）。

## 任务派发与状态

`muyan_pilot.py` 是最小 CLI，用于手工派活和查看队列。GitHub Issue 与标签是唯一状态存储，不引入数据库或 Web UI：

```bash
# 在第一个配置的 source repo 创建 Issue 并自动添加 ai-ready
python3 muyan_pilot.py add "任务标题" --body "任务描述" --config muyan-pilot.toml

# 派发到指定 source repo（必须在配置 source_repos 中）
python3 muyan_pilot.py add "任务标题" --repo xqliu/muyan-ceo --config muyan-pilot.toml

# 查看每个 source repo 的当前任务（ai-in-progress）、待办（ai-ready）和最近结果（ai-pr-opened / ai-fix-needed / ai-merged / ai-blocked）
python3 muyan_pilot.py status --config muyan-pilot.toml
```

`add` 成功后打印新 Issue 的 URL 和 `ai-ready` 标签；`status` 只读，不修改任何标签。命令失败立即报错，不做回退。

```bash
# 打印当前 run 的 Pi session 文件路径（repo_dir/.worktrees 下最新的 .pi-session/*.jsonl）
python3 muyan_pilot.py session --config muyan-pilot.toml

# 持续跟随该文件（等价 tail -f；跟的是命令启动时选中的文件，不中途跳到更新的文件）
python3 muyan_pilot.py session --follow --config muyan-pilot.toml
```

`session` 是排查附件（日常仍看 journal / GitHub），不是日常入口：没有 session 文件时 fail fast（退出码非零，说明没有正在跑的 Pi），不猜路径；`--pretty` 把 JSONL 打一行摘要（timestamp / role / tool|text|thinking 截断），默认仍是原始 JSONL。不开 tmux、不新包装脚本、不新增 systemd unit（Issue #74）。

```bash
# 幂等安装 systemd units（见「部署一致性」）：输出部署 commit 和每个 unit 的 sha256
python3 muyan_pilot.py install-units --config muyan-pilot.toml

# 只读部署/健康报告：repo commit、unit drift、timer/service active、
# Runner slot、Pi session、当前 Issue、最近 journal 活动
python3 muyan_pilot.py doctor --config muyan-pilot.toml

# 一次性 setup（新机器/新仓库）：gh auth + 仓库权限、平台 labels
# （labels.toml 为唯一事实源）、systemd user units、checkout 检查
# （含 Git transport：HTTPS origin 迁移为 SSH + SSH 连通性探测）、
# 可选模型 proxy（warning only）；幂等、fail-fast，--json 输出等价 JSON
python3 muyan_pilot.py setup --config muyan-pilot.toml
```

## GitHub Issue 标签（外部状态）

GitHub label 是仓库的**外部状态**：它不会随代码提交自动创建，缺失时扫描会静默漏掉对应状态的 Issue。新仓库/新机器用一次性 setup 入口完成初始化（幂等、fail-fast，label 名称/颜色/描述以仓库内的 `labels.toml` 为唯一事实源，已存在的 label 只做声明式对齐，业务 label 从不被改动；同时完成 Git transport 初始化：HTTPS `origin` 迁移为 SSH 并探测 SSH 连通性，见「Git transport」）：

```bash
# 一次性 setup：gh auth + 仓库权限、平台 labels、systemd user units、
# checkout 检查、可选模型 proxy（warning only）；输出 key=value（--json 等价 JSON）
python3 muyan_pilot.py setup --config muyan-pilot.toml
```

setup 不可用时的手工等价命令（已存在时 gh 会报错，可忽略或先 `gh label list` 检查）：

```bash
for l in ai-ready ai-in-progress ai-pr-opened ai-fix-needed ai-merged ai-blocked; do
  gh label create "$l" --repo xqliu/muyan-pilot --force
  gh label edit "$l" --repo xqliu/muyan-pilot \
    --description "Muyan Pilot delivery state (see README)"
done
# p0 是紧急优先级 label（不是交付状态，见「领取优先级（P0）」）
gh label create p0 --repo xqliu/muyan-pilot --force --color "fbca04" \
  --description "Muyan Pilot urgent priority: picked up before bugs and features"
gh label list --repo xqliu/muyan-pilot
```

| Label | 含义 | 进入条件 | 离开条件 |
|---|---|---|---|
| `ai-ready` | 明确派发给 Pilot 的新任务（允许 AI 领取） | `muyan_pilot.py add` 创建时自动添加，或人工 `gh issue edit --add-label ai-ready` | 领取时加 `ai-in-progress`（`ai-ready` 保留）；成功合并后保留（与 `ai-merged` 共存，表示已交付） |
| `ai-in-progress` | Runner 已领取、正在执行 | 领取 `ai-ready` Issue 时由 Runner 添加 | 开出 PR 时移除（转 `ai-pr-opened`）；失败时移除（转 `ai-blocked`）；Runner 被杀时残留，由下一 tick 的重启恢复扫描接回 |
| `ai-pr-opened` | PR 已创建，当前等待 review；不会自动再次启动 Fixer | `verify_pr` 验收通过后由 Runner 添加（同时移除 `ai-in-progress`） | clean verdict 合并后移除（转 `ai-merged`）；review finding / base 冲突时移除（转 `ai-fix-needed`）；终态失败时移除（转 `ai-blocked`） |
| `ai-fix-needed` | 已有 PR 需要在原 branch/worktree/PR 上继续修复；定时 Runner 自动拾取 | 审查会话未能修复的 finding，或 PR 落后最新 base / merge conflict | 下一个审查会话（同一 PR）clean verdict 合并后移除（转 `ai-merged`）；超轮 / 无法修复时移除（转 `ai-blocked`） |
| `ai-merged` | 成功终态：Runner 已合并 PR 并确认 merge commit 落在保护分支 | Runner 合并并确认后添加（替代 `ai-pr-opened`） | 终态，不再自动变更；PR body 的 `Fixes #N` 在 merge 时自动关闭 Issue |
| `ai-blocked` | Runner fail fast，需要人工处理；不会被自动拾取 | 命令失败、现场无法恢复、审查超轮等 fail fast 场景 | 人工修复现场并重新转为 `ai-fix-needed`（同一 PR）或重新领取（新 run）；不自动恢复 |
| `p0` | 紧急优先级（**不是交付状态**）：只改变领取顺序，不改变 Issue 粒度、交付状态或终态语义 | 人工加 label（生产链路出现高优先级故障时） | 人工移除；Runner 从不增删该 label |

## 任务依赖（blockedBy）

任务之间的依赖一律用 GitHub **原生依赖关系**（`blockedBy`/`blocking`，gh 2.94+）标注；**不要**在 Issue 正文写 `Depends on #N`——正文不进入 `blockedBy`，Runner 不解析正文依赖：

```bash
# 派发新 Issue 后标注依赖（N = 新 Issue，M = 前置 Issue）
gh issue edit N --repo xqliu/muyan-pilot --add-blocked-by M
# 解除依赖
gh issue edit N --repo xqliu/muyan-pilot --remove-blocked-by M
```

Runner 行为（单 slot 串行，只做“读字段-跳过-等待”，不引入 DAG、拓扑排序或多 worker 调度）：

- 领取 `ai-ready` Issue 前，Runner 读取原生 `blockedBy`（`gh issue list --json blockedBy`）；
- 存在未关闭 blocker（blocker 节点 `state=OPEN`）→ **不领取**：不加 `ai-in-progress`、不改任何标签、不建 worktree，记录结构化日志 `blocked_by issue=N repo=... blockers=M1,M2`，继续检查同 repo 后面的 ready Issue；
- blocker 关闭后不再阻塞：GitHub 会把该关系保留在列表中（节点 `state=CLOSED`，惰性，已对真实 API 验证），Runner 只统计未关闭 blocker——无需人工操作，下一 tick 该 Issue 自然可领；
- `blockedBy` 查询失败 → **fail open**（视为未阻塞：本 tick 不从该 repo 领取，记录 `blocked_by_check_failed`，下一 tick 重试查询），API 异常不会死锁队列。

## 领取优先级（P0）

紧急优先级用普通 GitHub label `p0` 表示（**不是交付状态**）：它只表达处理优先级，不改变 Issue 粒度（仍要求一个 Issue 对应一个 runtime outcome），不改变任何交付状态或终态语义，Runner 也从不增删该 label。

- ready 领取顺序固定为：`ai-ready`+`p0` → `ai-ready`+`bug` → 普通 `ai-ready`（三次 `gh issue list` 扫描，共享同一组排除条件和 `blockedBy` 语义）；
- P0 仍遵守全部现有排除规则（`ai-in-progress`、`ai-pr-opened`、`ai-fix-needed`、`ai-merged`、`ai-blocked`）和单 slot 约束：P0 被阻塞（open blocker）时跳过并回退到 bug/普通扫描，P0 已在途（`ai-in-progress`）时由重启恢复扫描接回（扫描同样携带 `labels`，恢复后进度评论继续显示 `p0`）；
- 领取日志行携带明确的 `priority=p0` / `priority=normal` 字段；GitHub 进度评论显示 `priority` 字段；run 现场（`run_info`）与 started milestone 携带 `priority=...`；
- P0 执行失败进入 `ai-blocked`（alone，移除领取标签）：`ai-ready` 标签残留被所有 ready 扫描排除，**没有任何 tick 会重新领取**，因此不会无限重试；失败评论和 blocked 现场保留具体失败原因和可恢复现场；
- P0 的 review/merge 失败沿用现有同一 PR、有限 review round（`MAX_REVIEW_ROUNDS=5`）机制，不引入新的循环。

## 自动可观测（正常运行不需要执行任何命令）

正常运行完全自动化：人不需要执行 status 命令、不需要轮询进程、不需要督工。
任务进入 GitHub Issue 池后，Runner 自己完成领取 → plan → implement → test →
verify → independent review（会话内修复）→ merge，并主动发布过程和最终结果。
`muyan_pilot.py status` 只保留为开发/故障排查附件，不是产品入口，也不能作为
自动可观测性的验收证据。

### journal（本地，systemd）

Pi 长时间运行时，Runner 不再只留下启动命令和最终结果。`bootstrap_runner.py` 运行 Pi 期间每 15 秒读取任务 worktree 里的 Pi session JSONL（`.pi-session/*.jsonl`），把简短活动写入 journal（systemd 日志）；已经打开 `journalctl -f` 时内容持续自动刷新。完整不变现场（branch / worktree / session 文件）只在 run 开始和失败时各记录一次，运行中只输出短的变化字段，避免每 15–30 秒重复整段上下文：

- `run_start run=... issue=owner/repo#n role=implement branch=... worktree=... session=... session_file=... phase=... last_activity=... action=... result=...`——run 开始时记录一次完整现场；
- `activity issue=... role=... phase=... action="..." result=... state=-|model_wait idle=...s`——phase/action/result 变化时输出（tool_result 只更新 result，不覆盖真实动作）；
- `heartbeat issue=... role=... phase=... state=-|model_wait elapsed=...m idle=...s`——没有变化时按轮询间隔输出，idle 直接写在行上；
- `model_wait issue=... role=... phase=... state=model_wait`——最近一条 session 事件是 tool result（模型正在等待响应）时输出一次；等待期间只按轮询间隔输出带 `state=model_wait` 的 heartbeat，不升级 WARNING（慢模型不等于卡死）；
- `resumed issue=... role=... phase=... state=resumed`——下一条 session 事件到达时输出一次。
- `pi_idle issue=... role=... phase=... stale_seconds=...`——超过 5 分钟（`PI_IDLE_WARN_SECONDS=300`）没有 model/session 活动且不在 `model_wait` 时输出一次 WARNING；卡住的 session 不会每个 heartbeat 重复告警，`model_wait` 期间永不告警（慢模型不等于卡死）；
- `pi_resumed issue=... role=... phase=...`——idle 告警后第一条新 session 事件到达时输出一次（恢复后输出 resumed）。
- `run_failed run=... issue=... role=... branch=... worktree=... session=... session_file=... phase=... ... reason=pi_exit_N|timeout_...s|upstream_dead_stale_...s`——进程异常退出、超时或上游已死（Issue #75）时先记录完整现场再抛出错误；
- `run_end run=... issue=... role=... result=pr_opened elapsed=...m pr=... commit=...`——验收通过后记录结果和完整排查入口。

上游已死检测（Issue #75）：`model_wait` 期间 session JSONL 冻结超过 `PI_MODEL_WAIT_DEAD_SECONDS`（默认 600 秒）判定上游（llama/proxy）已死——HTTP 超时或连接断开后 Pi 停在 epoll_wait 永不退出：Runner 杀掉 Pi 进程，记录 `run_failed ... reason=upstream_dead_stale_...s` 后 fail fast（Issue 标记 `ai-blocked`，现场留在 journal 和 Issue 评论，slot 随进程退出由内核释放，下一拍可 resume 或领取下一个 `ai-fix-needed`）。事件持续到达时永不触发（慢模型不是死上游），它也不是业务任务 timeout。

所有行都是稳定 `key=value`（含空格或双引号的值加双引号，内嵌双引号转义为 `\\"`，可用 `pi_activity.parse_scene` 解析）；systemd journal 已提供时间、host 和进程，Python 日志不再重复打印自己的时间戳。每条行都带 `[run_id]` 前缀（见下文全链路 run_id 一节），它是高频行（`activity` / `heartbeat` / `model_wait` / `resumed` / `pi_idle` / `pi_resumed`）唯一的 run id 载体：这些行不再重复 `run=` 字段，同一个 8-hex run id 在一行里只出现一次（Issue #57）。低频场景行（`run_start` / `run_failed` / `run_end`）保留 `run=` 字段，`pi_activity.parse_scene` 仍能从这些行解析出 `run`。默认 tail 示例（仅用于查看，不是产品步骤）：

```bash
journalctl --user -u muyan-pilot.service -f
# Aug 25 14:30:01 host muyan-pilot[123]: INFO [e07383c2] run_start run=e07383c2 issue=xqliu/muyan-pilot#18 role=implement branch=muyan-pilot/... worktree=/home/.../.worktrees/... session=sess-1 session_file=/home/.../.pi-session/sess-1.jsonl phase=starting last_activity=- action=- result=-
# Aug 25 14:30:16 host muyan-pilot[123]: INFO [e07383c2] activity issue=xqliu/muyan-pilot#18 role=implement phase=test action="bash pytest tests/" result=- state=- idle=6s
# Aug 25 14:30:31 host muyan-pilot[123]: INFO [e07383c2] heartbeat issue=xqliu/muyan-pilot#18 role=implement phase=test state=- elapsed=30s idle=15s
# Aug 25 14:30:32 host muyan-pilot[123]: INFO [e07383c2] activity issue=xqliu/muyan-pilot#18 role=implement phase=test action="bash pytest tests/" result=ok state=- idle=0s
# Aug 25 14:30:32 host muyan-pilot[123]: INFO [e07383c2] model_wait issue=xqliu/muyan-pilot#18 role=implement phase=test state=model_wait
# Aug 25 14:41:40 host muyan-pilot[123]: INFO [e07383c2] heartbeat issue=xqliu/muyan-pilot#18 role=implement phase=test state=model_wait elapsed=11m idle=11m
# Aug 25 14:42:19 host muyan-pilot[123]: INFO [e07383c2] activity issue=xqliu/muyan-pilot#18 role=implement phase=test action="assistant text" result=- state=- idle=0s
# Aug 25 14:42:19 host muyan-pilot[123]: INFO [e07383c2] resumed issue=xqliu/muyan-pilot#18 role=implement phase=test state=resumed
# Aug 25 16:02:10 host muyan-pilot[123]: WARNING [e07383c2] pi_idle issue=xqliu/muyan-pilot#18 role=implement phase=pr stale_seconds=5m
# Aug 25 16:03:05 host muyan-pilot[123]: INFO [e07383c2] pi_resumed issue=xqliu/muyan-pilot#18 role=implement phase=pr
# Aug 25 15:12:40 host muyan-pilot[123]: INFO [e07383c2] run_end run=e07383c2 issue=xqliu/muyan-pilot#18 role=implement result=pr_opened elapsed=42m pr=https://github.com/xqliu/muyan-pilot/pull/19 commit=0123456789abcdef0123456789abcdef01234567
```

每行都带 issue、run id、role（implement / review / merge）、phase、
elapsed、last activity、last action、session、branch。implementer、reviewer
两种 Pi session 用同一个机制观测，role 由 Runner 在启动 session 时传入。

### GitHub（手机，自动更新）

领取任务后，Runner 在 source Issue 上创建一条带隐藏 run marker
（`<!-- muyan-pilot:run=<run_id> -->`）的进度评论，之后只 PATCH 同一条
评论（进度变化时立即，且间隔不超过 30 秒），不新增 heartbeat 垃圾评论。
**Pi 运行期间就是活的**：implementer、reviewer 两种 session 的每次
活动变化/心跳都会渲染当前状态并 PATCH 同一条评论（不是等 Pi 退出后才
回写），手机用户看到的始终是进行中的进展，而不是一条静态的 starting
评论。评论始终显示：当前阶段、role、优先级（`p0`/`normal`，见「领取
优先级（P0）」）、已运行时间、最近活动时间、最近动作、
测试状态、review/fix round、branch、PR/merge 状态。进程重启后按 run marker
找回同一条评论继续更新，不需要数据库。

关键 milestone 单独发布简短评论（`Muyan Pilot: ...`），让 GitHub Mobile
主动推送通知：started、plan ready、tests passed/failed、review findings、
PR opened、merged、blocked。完成后进度评论更新为最终交付摘要
（PR、测试、审查证据）；真正失败时更新为 blocked 现场和下一步原因，同时
Issue 标记 `ai-blocked`。

### 调试附件

`muyan_pilot.py status` 只读展示当前（`ai-in-progress`）任务的实时状态（仅供开发/故障排查）：

```bash
python3 muyan_pilot.py status --config muyan-pilot.toml
# capacity: 1
# slots: 1/1
#   slot-1: pid=4321
# source: xqliu/muyan-pilot
#   base: main abc123def456
#   current: #24 Stream live Pi activity ... https://github.com/xqliu/muyan-pilot/issues/24
#     live: phase=test last_activity=2026-08-25T02:30:00Z action=bash pytest tests/ result=ok
#     session: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>/.pi-session/<session>.jsonl
#     worktree: .../.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-24-<run-id>
#   ready: -
#   result: -
```

顶部的 `capacity` / `slots` 是当前机器的并发容量（`max_concurrency`）与已占用 slot（含持有者 PID），见下一节。

journal、`status` 和 GitHub 进度评论只暴露脱敏摘要：完整 prompt、Issue body 和 token 不会写入日志或评论（命令日志固定为 `<redacted>`，工具摘要截断到 200 字符并屏蔽常见 token 形状）。关键阶段继续回写 GitHub Issue 评论：Pi 启动（含 branch 和 worktree）、PR 创建、失败现场（含完整 run 现场）。session JSONL 完整保留在 worktree 中，作为本地完整记录。

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

Runner 每次处理一个 delivery：领取（或恢复）一个 Issue 后，在整个 implement → review（会话内修复）→ merge 期间持有并发 slot，PR 合并或终态失败后退出，由 systemd timer 再次触发；不在 Python 内实现 daemon，不引入数据库、队列、重试或复杂恢复。没有人为的任务时长上限；命令错误立即失败，真正卡死时通过 systemd/journal 排查并人工停止。并发上限见下一节 `max_concurrency`：拿不到 slot 的 Runner 记录 `capacity_full` 后正常退出，不领取 Issue。

## 并发限制（max_concurrency）

本机允许的 Pilot 并发任务数由 `muyan-pilot.toml` 的 `max_concurrency` 配置：必须是正整数，缺失时默认 1（本地 AI/GPU 只能稳定服务一个任务）；非整数、布尔值、0 或负数启动即 fail fast。slot 状态在 `<repo_dir>/.muyan-pilot/slots/slot-N`（N = 1..max_concurrency）：每个 slot 文件是一个普通的锁目标，**文件上的排他 `flock(2)` 锁就是所有权**——内核保证同一时刻至多一个进程持有某个 slot 的锁。持有者 PID 只作为 `status` 展示的观察性元数据写入文件，不是所有权依据；没有 stale PID/超时回收启发式，没有 atexit/信号清理协议。

- 并发额度按完整任务生命周期计算：Runner 在领取 Issue 之前取得 slot，implement → review（会话内修复）→ merge 期间始终占用；PR 打开后任务没有结束，Runner 持有 slot 轮询 PR 状态（15 秒一次，与空闲 timer 同频）：PR `MERGED` 或终态失败（PR 未合并被关闭 → Issue 标记 `ai-blocked`）才释放 slot 退出；期间 Issue 进入 `ai-fix-needed`（review finding 或 base 冲突）时，**下一个 tick 在同一 run、同一 PR 上启动下一个独立审查会话**（会话内吸收最新 base 并修复 finding，见下节），不会冷启动 Fixer，也不会让新 Runner 插队领取新 Issue；
- 同一任务内部 implement/review 串行执行，共用同一个 slot，任意时刻最多一个 Pi 子进程；
- 达到 `max_concurrency` 时，新 Runner 不领取 Issue、不修改标签、不调用 Pi，记录结构化日志 `capacity_full max_concurrency=... slot_dir=...` 后正常退出（退出码 0），等 systemd timer 下次触发；
- slot 锁由打开的文件描述符持有：进程正常结束、SIGTERM/SIGINT（systemd stop / Ctrl+C）或被 SIGKILL 时，内核自动释放锁——活着的持有者永远不会因为时间或 PID 检查丢失 slot，死掉的持有者永远不会占住 slot，异常退出不会造成永久锁死；slot 文件本身保留（它是锁目标，不是令牌），`status` 按锁的实际状态报告占用；
- `muyan_pilot.py status` 显示配置容量和当前已占用 slot（`capacity: N`、`slots: k/N`、`slot-N: pid=...`）；占用判定用非阻塞 `flock` 探测（探测成功即空闲并立即解锁），PID 仅用于展示；
- 直接在 Pilot 外手工运行的任意 `pi` 命令不属于该配置控制范围：`max_concurrency` 只约束 Runner 领取任务时启动的 Pi，手工 `pi` 不受 slot 管理，也不会释放或占用任何 slot。

## 全链路 run_id（correlation ID）

每个任务 attempt 只生成一次 `run_id`（8 位 hex，例如 `e07383c2`），语义等同 trace ID：implement、review、merge 全部复用同一个值；同一个 Issue retry 时生成新的 run_id，Issue number 是多个 run 的共同父标识。不创建 `trace_id`/`log_id`/另一套 UUID，不引入 tracing backend。

同一个 run_id 出现在：

- 该 attempt 的每条 journal 日志首字段：`[e07383c2] command=...`；
- start / PR opened / failed 等 Issue 评论：可见字段 `run_id=e07383c2` + 隐藏 marker `<!-- muyan-pilot:run=e07383c2 -->`；
- feature branch 与 worktree 名（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14-a1b2c3d4`）；
- Pi session 目录（worktree 内 `.pi-session/`）与 plan/test/verify/review 等 run artifacts 的路径；
- 注入 Pi 的 prompt context（`Run id: ...`）；
- PR body 的稳定 machine-readable marker `<!-- muyan-pilot:run=e07383c2 -->`——Runner 验收时校验，缺失即 fail fast，拒绝该 PR；
- Pi 自己发出的 progress / review / 最终评论（prompt 要求携带同一 marker 和 `run_id=` 字段）。

查询方式（不依赖内存映射，进程重启后仍可恢复关联）：

```bash
# journal 中还原一个 run 的完整时间线
journalctl --user -u muyan-pilot.service | grep e07383c2

# GitHub 上搜索一个 run 的 progress / milestone / review / merge 记录
gh search issues "e07383c2" --repo xqliu/muyan-pilot

# 本地在 repo 中搜索 run_id 找到 worktree、session 和 run artifacts
grep -r e07383c2 .worktrees/   # 在 clone 根目录执行
```

缺少合法 run_id 的 run-scoped 事件（绑定 run、构建 GitHub marker、PR body 校验）会 fail fast，不做回退。

## 任务 base 与 worktree

每次领取任务前，Runner 在配置 repo 中执行 `git fetch origin <base_branch>`，并冻结 `origin/<base_branch>` 的精确 SHA（`base_branch` 在 TOML 中配置，默认 `main`）。任务 worktree 和 feature branch 都从该 SHA 创建，绝不使用主工作区当前 HEAD；branch 和目录名都带唯一 run 标识（例如 `.worktrees/muyan-pilot-xqliu-muyan-pilot-issue-14-a1b2c3d4`），同一个 Issue 返工时会生成新的独立 run，旧现场原样保留。base branch、base SHA 和 run 标识会写入 Issue 评论和 `status` 输出。任务 worktree 共享部署 checkout 的单一 `origin` remote（Git transport 为 SSH，见「Git transport」），worktree 内的 fetch/push（包括 workflow 文件）都走这条 SSH 通道。

### 重启恢复（kill 后自动续跑）

Runner 被 SIGKILL 时无法执行任何清理：任务 worktree 和 `ai-in-progress` 领取标签会留在 GitHub 上（Issue 仍带 `ai-ready`）。下一个 tick 的领取扫描在 ready 队列之前先扫描 `ai-ready`+`ai-in-progress`（且未处于其他交付状态）的 open Issue——**仅当没有其他 Runner 活着时**（其他 slot 被占用证明有活着的 Runner 在处理，此时 `ai-in-progress` 是“进行中”而不是“孤儿”，绝不为活着的 run 启动第二个 Pi；flock 锁是唯一事实源）。找到后走 `process_issue` 的恢复分支：复用最新 worktree 的 run id（branch、worktree、进度评论都由它驱动），按隐藏 run marker 找回同一条进度评论继续 PATCH，不新建 run、不新建 worktree、不新建评论。已完成 run 的 worktree 保留为证据但标签已移除，重新领取永远新 run。

Pi 创建 PR 前必须重新 fetch：若 `origin/<base_branch>` 已前进，需合入最新 base、手工解决冲突、重跑完整测试后再推送。Runner 在验收时用 `git merge-base --is-ancestor origin/<base_branch> HEAD` 验证最新远端 base 是交付 HEAD 的祖先；不满足则 fail fast，不接受 PR。不自动解决冲突，不 force push，不 merge 或 push 保护分支。

同一 worktree 下 Pi 新旧 `.pi-session` 的识别：恢复的 run（同一 worktree）会创建**新的** session JSONL，journal 只跟踪当前这次调用的 session（启动前已存在的 JSONL 永不跟随），避免把上一个 run 的 session 报告成活着的会话（Issue #45）。

`.worktrees/` 已加入 `.gitignore`，不会进入版本库。

## 自动 loop 与恢复现场（Issue #49）

自动 loop 的完整状态链（systemd timer 是唯一自动入口，不需要人工触发 CLI；一个本地 Pi slot 内始终只有一个执行进程）：

```text
ai-ready
  -> ai-in-progress          （领取，加标签，建 worktree，启动 Pi）
  -> ai-pr-opened            （PR 验收通过，等待 review）
  -> review                  （独立审查会话，会话内修复）
  -> ai-fix-needed           （未修复 finding / base 冲突；同一 PR 的下一个审查会话）
  -> review                  （下一个审查会话；`ai-fix-needed` 保留到合并，不重新加回 `ai-pr-opened`）
  -> merge                   （clean verdict + merge 门禁）
  -> ai-merged               （成功终态；Fixes #N 自动关闭 Issue）
```

规则：

- 只有两个 opened-PR 状态会被自动拾取：`ai-fix-needed`（下一个审查会话）和 `ai-pr-opened`（独立审查）；`ai-ready` 走领取，`ai-blocked` **不会自动恢复**（先要人工决策：修复现场后转 `ai-fix-needed` 或重新领取）；
- 修复必须沿用同一 PR number、branch、worktree、run_id（PR number 永远不变，head 可以前进）；
- base 前进时先 merge 最新 `origin/<base>`（冲突交给 Pi 解决），重跑全量测试后再继续；
- review 结论、测试、覆盖率、验证和结果都要回写 GitHub Issue/PR（`REVIEW_VERDICT` 评论、milestone、进度评论），不只留在本地；
- malformed recovery scene（缺少完整现场、无可信评论）fail fast：Issue 标记 `ai-blocked` 并写明具体原因，本 tick 停止，不做猜测。

恢复现场（resume scene）契约：

- Runner 开 PR 后在源 Issue 发布 `Muyan Pilot opened PR:` 评论，携带 run marker `<!-- muyan-pilot:run=<8 hex> -->`、可见 `run_id=` 字段、`base_branch`、`base_sha` 和 PR URL——这是恢复的唯一现场（这条 scene 评论不是旁路：它失败仍 fail fast，因为 resume 靠它）；
- 只有 **trusted maintainer**（OWNER/MAINTAINER/MEMBER/COLLABORATOR）发布的评论才可信；公开评论（authorAssociation=NONE）永远不可信，无法把 Runner 指向任意本地路径；
- branch 和 worktree 由配置的 repo、source repo、Issue 编号和 run_id **推导**，绝不从公开 comment 读取；
- **PR body 也必须包含稳定的 run marker**，否则恢复 fail fast（Runner 验收时校验）；
- **历史兼容**：统一 run_id 机制之前创建的旧 PR 可能只有 Issue comment marker、没有 PR body marker——恢复前需要**补齐 PR body marker**（编辑 PR body 加入 `<!-- muyan-pilot:run=<run_id> -->`），不能只改 label。

## PR body 契约：`Fixes #N`（Issue #53）

Pi 创建的 PR description 必须包含 `Fixes #<issue-number>`（可以放在首行），指向其源 Issue。GitHub 原生行为：PR merge 到默认分支（`main`）时，body（或 commit message）中的 `Fixes`/`Closes`/`Resolves #<n>` 关键词会自动关闭对应 Issue；PR title 不支持该关键词。没有这个关键词，merge 后 Issue 仍然 OPEN（例如 #45 的遗留状态），需要人工补关。

- prompt（`prompt.md`）在 PR 创建步骤要求携带 `Fixes #<issue-number>`（模板里的 `{{ISSUE_NUMBER}}` 由 Runner 渲染成真实 Issue 编号）；
- Runner 验收（`verify_pr`）校验 PR body 含 `Fixes #<issue-number>`，缺失即 fail fast 拒绝该 PR（`pr_fixes_missing`），与 run marker 校验同级；
- 开发契约见 `AGENTS.md`（Git 一节）。

## PR 创建后的 review/修复 循环（Issue #45、#82）

PR 创建后任务没有结束：Issue 进入可恢复的 review 状态。`ai-pr-opened` 表示**等待 review**；`ai-fix-needed` 表示当前 head 尚不可合并（审查会话未能修复的 finding，或 PR 落后最新 base / 存在 merge conflict）——下一个 tick 在同一 run、同一 PR 上启动**下一个独立审查会话**，由审查会话在会话内吸收最新 base 并修复 finding（Issue #82：不再冷启动 Fixer，也没有第三次 review）。Review finding、base 前进或 merge conflict 都是可修复状态，不等于任务失败，也不重新进入 ready 队列。

- 每个 tick 先按顺序扫描 source repos 中两个 opened-PR 状态的 open Issue（Issue #70）：`ai-fix-needed`（finding 或 base 冲突 → 同一 PR 的下一个审查会话）和 `ai-pr-opened`（等待 review → 同一 PR 的独立审查）。`ai-pr-opened` 被扫描是因为开 PR 的那次 delivery 可能已经不在（Runner 被杀，或 Issue #70 背后的进度 PATCH 404 曾把已交付的 Issue 在审查开始前就标成 `ai-blocked`）：没有这个扫描，一个有效的 MERGEABLE PR 会永远没有 owner。`ai-blocked` 的 Issue 被排除（先要人工决策），`ai-merged` 和 `ai-in-progress` 同样被排除；找到时，Runner 只信任由维护者（OWNER/MAINTAINER/MEMBER/COLLABORATOR）发布的最新 `Muyan Pilot opened PR:` 评论（公开评论永远不可信），从中恢复 run 现场（`run_id`、base_branch、base_sha、PR URL），branch 和 worktree 由配置的 repo、Issue 编号和 run_id **推导**（绝不从评论读取，评论无法指定任意本地路径），在**原 worktree、原 branch、同一 PR** 上继续（`ai-fix-needed` 与 `ai-pr-opened` 都直接进入审查等待），而不是领取新 Issue。现场无法恢复（评论缺少完整现场、无可信评论）时 fail fast：Issue 标记 `ai-blocked` 并写明具体原因，本 tick 停止，不做猜测，也不让新任务插队。任何 git/Pi 变更前，Runner 先校验配置的 base 和 open PR（head repo、head branch、base、run marker、精确 URL）。
- 审查会话（journal 中 `role=review`）在会话内完成修复：修改代码、重跑完整测试与 100% 行/分支覆盖率、commit 并**只 push 原 task branch**，然后对修复后的 head 重新输出 `REVIEW_VERDICT`。PR 头分支前进，**PR number 保持不变**。
- 审查会话无法修复的 finding（或 PR 落后最新 base / merge conflict）：Issue 标记 `ai-fix-needed`，等待下一个 tick 的下一个审查会话（会话内 `git merge origin/<base>` 吸收最新 base、解决冲突、全量回归后重新输出 verdict）。Runner 自己不自动解决冲突、不 `--abort`、不 force push、不 push 保护分支。
- 审查/修复循环最多 5 轮（见 `MAX_REVIEW_ROUNDS`）：超轮仍有 Blocker/Major、审查 Pi 失败或无法验证时，Issue 标记 `ai-blocked`（移除 opened-PR 状态），评论写明具体失败和完整现场；PR、branch、worktree 原样保留，不删除、不关闭、不重建。
- Runner/服务重启后，仅凭 Issue 标签、评论 marker、PR head、branch 和 worktree 即可恢复该循环；implement/review/merge 串行占用同一个并发 slot，不会为同一个 run 启动第二个 Pi。

状态语义：

```text
ai-in-progress → PR opened (ai-pr-opened) → review（会话内修复）
  → 未修复的 finding / base 冲突 (ai-fix-needed) → 下一个审查会话
  → clean verdict → merge → ai-merged
```

## 自动审查、修复与合并（Issue #34、#82）

Pi 不直接 push 保护分支。实现 Agent 只 push feature branch 并创建 PR；PR 打开后由 **Runner** 在持有 slot 的交付等待循环中关闭闭环：

1. **冻结 PR 的 base/head SHA**（`gh pr list` 取唯一 open PR 的 `baseRefOid`/`headRefOid`）。
2. **独立审查（同时是修复者，Issue #82）**：启动一个独立的 Review Agent（code-review R1–R9，不附加 `review-fix-loop`/`tdd-dev` skill），对精确 base/head SHA 审查需求、diff、调用链、测试与运行证据；审查会话与 implementer 一样通过 live activity 管道输出（journal 中 `role=review`）。审查者**可以修改代码**：发现 Blocker/Major 时在同一会话内修复、重跑完整测试与 100% 行/分支覆盖率、commit 并只 push task branch，然后对修复后的 head 重新输出 verdict——没有冷启动 Fixer，也没有第三次 review。审查会话必须以一行机器可读的 `REVIEW_VERDICT {"verdict":"pass|findings","blockers":N,"majors":N,"minors":N,"findings":[...]}` 结尾；`pass` 表示**会话内修复之后**零 Blocker/Major；读不到合法 verdict 一律 fail fast，绝不当作通过。
3. **重新冻结并合并门禁**：clean verdict 后 Runner 重新冻结 PR（审查者可能已 push 修复，head 前进），重新 fetch 最新 `origin/<base>`，要求 PR head 包含最新 base、PR mergeable、远端 head 仍是被审查的 head；然后 `gh pr merge <n> --match-head-commit <head> --merge`，只有被审查的 head 能落地。
4. **确认合并并同步部署 checkout**：`gh pr view` 确认 PR `MERGED` 且 `mergeCommit` 已落在 `origin/<base>`；随后把配置 `repo_dir` 的 base checkout `git merge --ff-only origin/<base>` 并验证本地 HEAD == `origin/<base>`，下一个 systemd tick 加载的就是刚合并的新代码。Issue 由 PR body 的 `Fixes #N` 关键词在 merge 时自动 CLOSED（见上方「PR body 契约」）。
5. **未合并的 head**：审查会话未能修复的 finding（或 PR 落后最新 base / merge conflict）→ Issue 标记 `ai-fix-needed`，下一个 tick 在同一 PR 上启动下一个审查会话（会话内吸收最新 base、解决冲突、全量回归、重新输出 verdict）。审查循环最多 5 轮（见 `MAX_REVIEW_ROUNDS`）；超轮仍有 Blocker/Major、审查 Pi 失败或无法验证时 fail fast 并标记 `ai-blocked`。

成功合并后 Issue 标记 `ai-merged`（替代 `ai-pr-opened`），评论写入 PR URL、merge commit、审查轮次和 base/run 信息。下一任务只从新的 `origin/<base>` 创建。不 force push、不直接 push 保护分支、不设业务 timeout；审查 finding 不是 `ai-blocked`，而是在同一 PR 的审查会话内修复（或交给下一个审查会话）。

两个 prompt 由配置提供（默认 `prompt.md` 实现、`prompt_review.md` 审查+会话内修复）；审查 prompt 不附加 `review-fix-loop`/`tdd-dev` skill。

## 许可证

本项目以 [Apache License 2.0](LICENSE)（SPDX 标识 `Apache-2.0`）发布，完整文本见根目录 [LICENSE](LICENSE) 文件。
