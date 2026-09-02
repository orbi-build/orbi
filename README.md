# Orbi

Orbi 是本地 AI 开发 Worker（v0.3.0 起对外品牌为 Orbi，GitHub 项目为
[`orbi-build/orbi`](https://github.com/orbi-build/orbi)，官网 <https://orbi.build>，
文档站 <https://docs.orbi.build>）。最小 bootstrap：从配置文件中的 source
repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发
并创建 PR；随后 Runner 自动完成独立审查（会话内修复）和合并。

**品牌与兼容（v0.3.0 rebrand，Issue #183）**：对外品牌统一为 Orbi（README、
文档站、仓库描述、package metadata、对外链接）；已有用户的使用方式不变——CLI
仍是 `muyan-pilot`、配置文件仍是 `muyan-pilot.toml`、systemd unit 仍是
`muyan-pilot@*`、运行时 label（`ai-*`/`p0`）与 run marker
（`<!-- muyan-pilot:run=... -->`）均不改动。

开发契约见 [AGENTS.md](AGENTS.md)；面向新用户的完整操作说明（安装前提、
配置、首次启动、smoke walkthrough、工作流、运维、安全、测试、贡献）在文档站
<https://docs.orbi.build>（仓库内 [`docs/`](docs/) 是唯一事实源，Mintlify 只
构建、搜索和托管；中文入口 [`docs/zh/`](docs/zh/)）。本 README 只保留项目定位、
快速开始、核心工作流和关键入口；每条规则的完整说明只在一个事实源里，其余位置
只做短引用。

## 快速开始

```bash
# 1. clone
git clone https://github.com/orbi-build/orbi.git
cd orbi

# 2. 安装 CLI（editable uv tool 安装，官方本地部署方式）
uv tool install --force --reinstall --editable --python /usr/bin/python3 .
muyan-pilot --help

# 3. 创建配置（仓库只提交 example，真实配置本地维护）
cp .muyan-pilot.example.toml muyan-pilot.toml

# 4. 一次性 setup（gh auth + 仓库权限、平台 labels、systemd user units、
#    checkout 检查含 Git transport；幂等、fail-fast）
muyan-pilot setup --config muyan-pilot.toml

# 5. 手动跑一个 tick（首次验证/排查；日常由 timer 调度）
python3 bootstrap_runner.py --config muyan-pilot.toml

# 6. 验证 timer 与部署健康
systemctl --user list-timers 'muyan-pilot@*.timer'
muyan-pilot doctor --config muyan-pilot.toml
```

新用户从 [Getting started](docs/getting-started.mdx) 可以完整走完安装、配置、
setup、首次运行和 doctor 验证（含从 0 开始的 smoke walkthrough 与故障排查表）；
一次性 setup 的完整输出契约与失败现场见
[One-time setup](docs/setup.mdx)。

## 当前运行

正常运行使用 systemd user timer，全天 24 小时运行，每 5 分钟自动执行一次
（触发点覆盖 00:00–23:55）：

```bash
# 幂等安装用户级 service/timer 模板（仓库模板复制到用户 systemd 目录、
# daemon-reload，并按 max_concurrency 启用对应的 timer 实例），并输出部署
# commit 和每个 unit 的 sha256：
muyan-pilot install-units --config muyan-pilot.toml
systemctl --user list-timers 'muyan-pilot@*.timer'
```

安装按 `max_concurrency` 同步 timer：`1` 只启用 `muyan-pilot@1.timer`，`2`
启用 `muyan-pilot@1.timer` 和 `muyan-pilot@2.timer`。`install-units` 是幂等的：
重复执行只会把仓库模板重新复制到位并 `daemon-reload`，**不会**启动、停止或重启
正在运行的 Runner（新配置从下一次 service 启动生效）。timer、`ExecStartPre`
代码同步、unit 漂移自愈、slot 并发与故障恢复的完整说明见
[Operations](docs/operations.mdx)。

## 代码更新与部署一致性

service 每次真正启动时，先由 `ExecStartPre` 在 Python Runner 进程外执行：

```bash
timeout 90s git fetch --no-auto-maintenance origin main && git merge --ff-only origin/main
```

本地 main 被 fast-forward 到最新 `origin/main` 后，Runner 才用新代码启动；
当前正在运行的长任务不会被热更新、不会被杀。仓库中的
`systemd/muyan-pilot@.service` 和 `systemd/muyan-pilot@.timer`（模板 unit）是
已安装 unit 的**唯一事实源**；Runner 每次启动在领取任何 Issue 之前对比已安装
unit 与模板，漂移用同一个幂等安装自愈并复核（`unit_drift auto_synced`），
复核后仍漂移则记录结构化日志并 fail fast（不取 slot、不领取）：

```text
unit_drift auto_synced unit=muyan-pilot@.timer before_sha256=... after_sha256=... commit=<deployed HEAD>
unit_drift unit=muyan-pilot@.timer repo=<repo path> installed=<installed path> repo_sha256=... installed_sha256=... fix=muyan-pilot install-units
```

安装同时**一次性迁移** #149 之前的非模板 unit（`systemd/muyan-pilot.service` /
`systemd/muyan-pilot.timer`：`systemctl --user disable --now muyan-pilot.timer`
停的是 timer，绝不停/启/重启 service）并删除旧文件；已迁移过的机器上是 no-op。
`muyan-pilot doctor` 是只读诊断（repo commit、unit drift、timer/service active、
slot、Pi session、当前 Issue、最近 journal 活动），不改标签、不改 unit、不做
git 变更。

**完整部署时序**（模板变更的 PR 合并到 main 后不需要任何人工步骤）：

```text
git merge 到 main（含 unit 模板变更）
  -> timer 下一次触发
  -> ExecStartPre 同步 origin/main（fetch + fast-forward）
  -> 启动前 unit 漂移检查（漂移则幂等自愈 + 复核，见上；仍漂移才 fail fast）
  -> 启动前 Git transport 检查（SSH 且可达才继续，见下节）
  -> Runner 启动并执行一个 Issue
```

## Git transport（Issue #114）

两条认证通道，职责边界清晰：**Git 数据操作**（fetch、push——包括推送
`.github/workflows/*.yml`）走 **SSH**（`git@github.com:owner/repo.git`，本机
SSH key）；**GitHub API 操作**（Issue、PR、label、comment、merge）走 `gh`
token。SSH 从不作为 API 认证，`gh` token 也从不用于 git 数据。

Runner 每次启动（取 slot/领取任何 Issue 之前）校验 checkout 的 transport：
配置的 `origin` URL 必须是第一个配置 source repo 的 SSH 形式，且
`git ls-remote <ssh-url>` 退出码 0。失败时记录结构化日志并 fail fast
（`transport_check_failed ... reason=...`，不取 slot、不领取、不改标签），
**不自动降级到 HTTPS，也不静默跳过 workflow 文件**。已有 HTTPS remote 的迁移只
由人工执行的一次性 setup 入口完成（`muyan-pilot setup`，内部执行
`git remote set-url origin git@github.com:owner/repo.git`）；其他路径遇到
HTTPS remote 时 fail fast 并携带确切的迁移命令。完整说明见
[Operations](docs/operations.mdx) 与 [One-time setup](docs/setup.mdx)。

## CLI 安装与升级（Issue #140、#152、#158）

正式使用方式是 **editable** `uv tool` 安装的可执行 CLI（console script
`muyan-pilot = muyan_pilot:main`，见 `pyproject.toml`）：

```bash
uv tool install --force --reinstall --editable --python /usr/bin/python3 <deployment checkout>
muyan-pilot --help
muyan-pilot --version
```

editable 安装下 tool 环境直接从部署 checkout 导入 `muyan_pilot`，
`ExecStartPre` 把 checkout 同步到最新 main 后下一个 CLI 进程自动取到最新代码：
**普通 Python 源码与 systemd 模板/迁移代码变更不需要任何重装或升级命令**。打包
元数据变更（`pyproject.toml` 的 `py-modules`、入口点、版本、依赖）会让已安装的
finder 过期——Runner 每次启动在任何 slot/claim 之前自动刷新：打包指纹（checkout
`pyproject.toml` 的 sha256）未变则完全不跑 `uv`；变更或首次安装时在 base-sync
flock 保护下跑一次上面的 force editable 重装；安装失败 fail fast（结构化
`cli_install_failed` 行：原因 + 精确的修复命令），不取 slot、不 claim、不改标签。
`muyan-pilot doctor` 报告 CLI 源码一致性（`cli_source: clean` 或
`cli_source: DRIFT` + 结构化 `cli_source_drift` 行）。`muyan_pilot.py` 的直接
执行入口保留为开发/兼容路径，不是正式使用方式。完整说明（为什么必须 editable、
#152/#158 事故背景）见 [Getting started](docs/getting-started.mdx)。

## 任务派发与状态

`muyan-pilot` 是最小 CLI，用于手工派活和查看队列。GitHub Issue 与标签是唯一
状态存储，不引入数据库或 Web UI：

```bash
# 在第一个配置的 source repo 创建 Issue 并自动添加 ai-ready
muyan-pilot add "任务标题" --body "任务描述" --config muyan-pilot.toml

# 查看每个 source repo 的当前任务（ai-in-progress）、待办（ai-ready）和
# 最近结果（ai-pr-opened / ai-fix-needed / ai-merged / ai-blocked）
muyan-pilot status --config muyan-pilot.toml

# 打印当前 run 的 Pi session 文件路径（repo_dir/.worktrees 下最新的
# .pi-session/*.jsonl）；--follow 持续跟随该文件
muyan-pilot session --config muyan-pilot.toml
```

`add` 成功后打印新 Issue 的 URL 和 `ai-ready` 标签；`status` 只读，不修改任何
标签；`session` 是排查附件（跟的是命令启动时选中的文件，不中途跳到更新的文件），
没有 session 文件时 fail fast。命令失败立即报错，不做回退。

## GitHub Issue 标签（外部状态）

GitHub label 是仓库的**外部状态**：它不会随代码提交自动创建，缺失时扫描会静默
漏掉对应状态的 Issue。新仓库/新机器用一次性 setup 入口完成初始化（幂等、
fail-fast，label 名称/颜色/描述以仓库内的 `labels.toml` 为唯一事实源，已存在的
label 只做声明式对齐，业务 label 从不被改动）：

```bash
muyan-pilot setup --config muyan-pilot.toml
```

setup 不可用时的手工等价命令（已存在时 gh 会报错，可忽略或先 `gh label list`
检查）：

```bash
for l in ai-ready ai-in-progress ai-pr-opened ai-fix-needed ai-merged ai-blocked; do
  gh label create "$l" --repo orbi-build/orbi --force
  gh label edit "$l" --repo orbi-build/orbi \
    --description "Orbi delivery state (see README)"
done
# p0 是紧急优先级 label（不是交付状态，见「领取优先级（P0）」）
gh label create p0 --repo orbi-build/orbi --force --color "fbca04" \
  --description "Orbi urgent priority: picked up before bugs and features"
# ai-epic 是 Epic 协调 label（不是交付状态，见 Workflow 文档）
gh label create ai-epic --repo orbi-build/orbi --force --color "bfdadc" \
  --description "Epic coordination issue; not directly executable by Runner"
# ai-release 是 Release task 标记（不是交付状态，见 Workflow 文档）
gh label create ai-release --repo orbi-build/orbi --force --color "5319e7" \
  --description "Release task: Runner runs the deterministic release state machine (tag + GitHub Release)"
# ai-ticket-only 是内容交付标记（不是 Git 交付状态）
gh label create ai-ticket-only --repo orbi-build/orbi --force --color "0e8a16" \
  --description "Content task: Agent posts the deliverable directly to the Issue without Git delivery"
gh label list --repo orbi-build/orbi
```

| Label | 含义 | 进入条件 | 离开条件 |
|---|---|---|---|
| `ai-ready` | 明确派发给 Pilot 的新任务（允许 AI 领取） | `muyan-pilot add` 创建时自动添加，或人工 `gh issue edit --add-label ai-ready` | 领取时加 `ai-in-progress`（`ai-ready` 保留）；成功合并后保留（与 `ai-merged` 共存，表示已交付） |
| `ai-in-progress` | Runner 已领取、正在执行 | 领取 `ai-ready` Issue 时由 Runner 添加；resume opened-PR 交付（`ai-pr-opened` / `ai-fix-needed`）时由 Runner 幂等回填后再继续（Issue #178） | 开出 PR 时移除（转 `ai-pr-opened`）；失败时移除（转 `ai-blocked`）；Runner 被杀时残留，由下一 tick 的恢复扫描接回 |
| `ai-pr-opened` | PR 已创建，当前等待 review；不会自动再次启动 Fixer | `verify_pr` 验收通过后由 Runner 添加（同时移除 `ai-in-progress`） | clean verdict 合并后移除（转 `ai-merged`）；review finding / base 冲突 / **可恢复失败**（Pi 执行失败、验证失败、未推送本地 commit、worktree 缺失等，Issue #50）时移除（转 `ai-fix-needed`）；只有**不可恢复失败**（Issue #50）时移除（转 `ai-blocked`） |
| `ai-fix-needed` | 已有 PR 需要在原 branch/worktree/PR 上继续修复；定时 Runner 自动拾取 | 审查会话未能修复的 finding，或 PR 落后最新 base / merge conflict，或已有 run/PR 的**可恢复失败**（Issue #50：失败 comment 写入 Issue 和 PR，带 run_id、PR、branch、worktree、session、phase、last activity 和具体错误） | 下一个审查会话（同一 PR，同一 run_id/branch/worktree）clean verdict 合并后移除（转 `ai-merged`）；审查超轮（`MAX_REVIEW_ROUNDS`）时移除（转 `ai-blocked`，超轮是人工决策） |
| `ai-merged` | 成功终态：Runner 已合并 PR 并确认 merge commit 落在保护分支 | Runner 合并并确认后添加（替代 `ai-pr-opened`） | 终态，不再自动变更；PR body 的 `Fixes #N` 在 merge 时自动关闭 Issue |
| `ai-blocked` | Runner fail fast，需要人工处理；不会被自动拾取 | **只用于 AI 无法安全判断或修复的外部前置条件**（Issue #50）：无可恢复的 opened-PR 现场（缺少/不可信 `Muyan Pilot opened PR` 评论）、base 分支变更、审查超轮、PR 未合并被关闭；进入时 comment 必须写明为什么不能自动恢复。已有 run/PR 的可恢复失败**不**进入此状态（转 `ai-fix-needed`，见上） | 人工修复现场并重新转为 `ai-fix-needed`（同一 PR）或重新领取（新 run）；不自动恢复 |
| `p0` | 紧急优先级（**不是交付状态**）：只改变领取顺序，不改变 Issue 粒度、交付状态或终态语义 | 人工加 label（生产链路出现高优先级故障时） | 人工移除；Runner 从不增删该 label |
| `ai-epic` | Epic 协调 Issue（发布清单/多任务聚合，**不是可执行任务、不是交付状态**）：只负责聚合与发布门禁 | 人工加 label（创建 Epic 时） | 人工移除（Epic 完成并关闭时）；Runner 从不增删该 label，也从不领取带它的 Issue |
| `ai-release` | Release task 标记（**不是交付状态**）：带它的 `ai-ready` Issue 由 Runner 的确定性 release 状态机处理（tag + GitHub Release），**从不进 `run_pi` 开发路径**（Issue #98） | 人工加 label（派发 release task 时，与 `ai-ready` 同时） | 成功发布后 Runner 加 `ai-merged`（终态）并关闭 Issue；任何失败单独转 `ai-blocked`（不自动重试，人工决策点）；Runner 从不增删该 label 本身 |
| `ai-ticket-only` | 内容任务标记（**不是 Git 交付状态**）：Agent 仅生成最终内容并直接评论到 source Issue，绝不创建 worktree、branch、commit、PR 或内容文件（Issue #209） | 人工与 `ai-ready` 一起添加；任务类型只由此 label 明确指定，绝不从标题或正文推断 | 成功后 Runner 关闭 Issue 并移除 `ai-in-progress`；失败转 `ai-blocked`，评论保留 run_id 和具体失败现场；Runner 从不添加或移除此 type label |

标签语义与领取扫描的完整行为（blockedBy 依赖、P0 领取顺序、Epic 跳过、Release
状态机）见 [Workflow](docs/workflow.mdx)。

## 领取优先级（P0）

紧急优先级用普通 GitHub label `p0` 表示（**不是交付状态**）：它只表达处理
优先级，不改变 Issue 粒度、任何交付状态或终态语义，Runner 也从不增删该 label。

- ready 领取顺序固定为：`ai-ready`+`p0` → `ai-ready`+`bug` → 普通 `ai-ready`
  （三次 `gh issue list` 扫描，共享同一组排除条件和 `blockedBy` 语义）；
- P0 仍遵守全部现有排除规则（`ai-in-progress`、`ai-pr-opened`、`ai-fix-needed`、
  `ai-merged`、`ai-blocked`）和单 slot 约束：P0 被阻塞（open blocker）时跳过并
  回退到 bug/普通扫描，P0 已在途时由重启恢复扫描接回；
- P0 执行失败进入 `ai-blocked`（alone，移除领取标签）：`ai-ready` 标签残留被
  所有 ready 扫描排除，**没有任何 tick 会重新领取**，因此不会无限重试；
- 可选配置 `active_milestone`（Milestone 标题）把新领取扫描限制在一个版本内，
  P0 不跨 Milestone；恢复态（已开 PR、在途重启）不受 Milestone 限制。

完整说明见 [Workflow](docs/workflow.mdx)。

## 自动 loop 与恢复现场（Issue #49）

自动 loop 的完整状态链（systemd timer 是唯一自动入口；一个本地 Pi slot 内始终
只有一个执行进程）：

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

- 只有两个 opened-PR 状态会被自动拾取：`ai-fix-needed`（下一个审查会话）和
  `ai-pr-opened`（独立审查）；`ai-ready` 走领取，`ai-blocked` **不会自动恢复**
  （先要人工决策：修复现场后转 `ai-fix-needed` 或重新领取）；
- 修复必须沿用同一 PR number、branch、worktree、run_id（PR number 永远不变，
  head 可以前进）；
- base 前进时先 merge 最新 `origin/<base>`（冲突交给 Pi 解决），重跑全量测试
  后再继续；
- review 结论、测试、覆盖率、验证和结果都要回写 GitHub Issue/PR
  （`REVIEW_VERDICT` 评论、milestone、进度评论），不只留在本地。

恢复现场（resume scene）契约：

- Runner 开 PR 后在源 Issue 发布 `Muyan Pilot opened PR:` 评论，携带 run marker
  `<!-- muyan-pilot:run=<8 hex> -->`、可见 `run_id=` 字段、`base_branch`、
  `base_sha` 和 PR URL——这是恢复的唯一现场（这条 scene 评论不是旁路：它失败仍
  fail fast，因为 resume 靠它）；
- 只有 **trusted maintainer**（OWNER/MAINTAINER/MEMBER/COLLABORATOR）发布的评论
  才可信；公开评论（authorAssociation=NONE）永远不可信，无法把 Runner 指向任意
  本地路径；
- branch 和 worktree 由配置的 repo、source repo、Issue 编号和 run_id **推导**，
  绝不从公开 comment 读取；
- **PR body 也必须包含稳定的 run marker**，否则恢复 fail fast（Runner 验收时
  校验）；
- **历史兼容**：统一 run_id 机制之前创建的旧 PR 可能只有 Issue comment marker、
  没有 PR body marker——恢复前需要**补齐 PR body marker**（编辑 PR body 加入
  `<!-- muyan-pilot:run=<run_id> -->`），不能只改 label。

同一 worktree 下 Pi 新旧 `.pi-session` 的识别：恢复的 run（同一 worktree）会
创建**新的** session JSONL，journal 只跟踪当前这次调用的 session（启动前已存在
的 JSONL 永不跟随），避免把上一个 run 的 session 报告成活着的会话（Issue #45）。

## PR body 契约：`Fixes #N`（Issue #53）

PR description 必须包含 `Fixes #<issue-number>`（可以放在首行），指向其源
Issue。GitHub 原生行为：PR merge 到默认分支（`main`）时，body（或 commit
message）中的 `Fixes`/`Closes`/`Resolves #<n>` 关键词会自动关闭对应 Issue；PR
title 不支持该关键词。prompt（`prompt.md`）在 PR 创建步骤要求携带
`Fixes #<issue-number>`；Runner 验收（`verify_pr`）校验 PR body 含
`Fixes #<issue-number>`，缺失即 fail fast 拒绝该 PR（`pr_fixes_missing`），与
run marker 校验同级。开发契约见 `AGENTS.md`（Git 一节）。

## 自动审查、修复与合并（Issue #34、#82）

实现 Agent 在提交处停止（不 fetch、不 push、不创建 PR）；Runner 完成确定性
收口（Issue #186：base fetch 与吸收、plain push task branch、创建 PR），PR
打开后由 **Runner** 在持有 slot 的交付等待循环中关闭闭环：

1. **冻结 PR 的 base/head SHA**；
2. **独立审查（同时是修复者，Issue #82）**：独立的 Review Agent
   （code-review R1–R9）对精确 base/head SHA 审查；发现 Blocker/Major 时在同一
   会话内修复、重跑完整测试与分层覆盖率门禁（Issue #234）、commit 并只
   push task branch，然后对修复后的 head 重新输出 verdict——没有冷启动
   Fixer，也没有
   第三次 review。审查会话必须以一行机器可读的 `REVIEW_VERDICT` 结尾；`pass`
   表示**会话内修复之后**零 Blocker/Major；读不到合法 verdict 一律 fail fast，
   绝不当作通过；
3. **重新冻结并合并门禁**：clean verdict 后 Runner 重新冻结 PR，重新 fetch 最新
   `origin/<base>`，要求 PR head 包含最新 base、PR mergeable、远端 head 仍是
   被审查的 head；然后 `gh pr merge <n> --match-head-commit <head> --merge`，
   只有被审查的 head 能落地；
4. **确认合并**：`gh pr view` 确认 PR `MERGED` 且 `mergeCommit` 已落在
   `origin/<base>`；Issue 由 PR body 的 `Fixes #N` 关键词在 merge 时自动
   CLOSED；
5. **未合并的 head**：审查会话未能修复的 finding（或 PR 落后最新 base / merge
   conflict）→ Issue 标记 `ai-fix-needed`，下一个 tick 在同一 PR 上启动下一个
   审查会话（会话内吸收最新 base、解决冲突、全量回归、重新输出 verdict）。已有
   run/PR 的**可恢复失败**（Pi 执行失败、验证失败、未推送本地 commit、worktree
   缺失等，Issue #50）同样标记 `ai-fix-needed`：失败 comment 写入 Issue 和
   PR（带完整现场），下一个 tick 在同一 run、branch、worktree、PR 上继续，不
   重新创建 PR。审查循环最多 5 轮（`MAX_REVIEW_ROUNDS`）；**只有**超轮仍有
   Blocker/Major 时 fail fast 并标记 `ai-blocked`（超轮是人工决策）。

成功合并后 Issue 标记 `ai-merged`（替代 `ai-pr-opened`），评论写入 PR URL、
merge commit、审查轮次和 base/run 信息。不 force push、不直接 push 保护分支、
不设业务 timeout。完整链路与状态语义见 [Workflow](docs/workflow.mdx)。

## 自动可观测（正常运行不需要执行任何命令）

正常运行完全自动化：人不需要执行 status 命令、不需要轮询进程、不需要督工。
Runner 自己完成领取 → plan → implement → test → verify → independent review
（会话内修复）→ merge，并主动发布过程和最终结果（journal + GitHub 进度评论）。
`muyan-pilot status` 只保留为开发/故障排查附件，不是产品入口，也不能作为自动
可观测性的验收证据。

journal（本地，systemd）是运行中的实时记录：每条行都带 `[run_id]` 前缀、
issue、role（implement/review/merge）、phase、elapsed、last activity、last
action、session、branch；`journalctl --user -u 'muyan-pilot@*.service' -f`
持续自动刷新。模型请求挂死检测（Issue #75/#218，阈值可配置，Issue #228）：
`model_wait` 期间 session JSONL 冻结超过配置的 `model_wait_dead_seconds`
（默认 `PI_MODEL_WAIT_DEAD_SECONDS` = 1800 秒；Issue #228 前默认 600 秒）即
判定该次模型请求挂死——模型服务进程活着、连接还 ESTABLISHED 都不是豁免
（进程活着 ≠ 在回话）。Runner 先记一行结构化
`model_wait_dead issue=... role=... idle_seconds=... threshold=...
action=kill_pi session=... run_id=... upstream_alive=true|false
reason=hung_model_request`（`upstream_alive` 是证据，写进日志供排查，永不
否决 kill），再杀掉 Pi 会话，记录 `run_failed ... reason=model_wait_dead_stale_...s`
后 fail fast（implement 阶段 Issue 标记 `ai-blocked`、review 阶段标记
`ai-fix-needed`）。事件持续到达时永不触发：慢生成（session 事件不断刷新 stale
计时）不是挂死请求。它也不是业务任务 timeout。

所有 journal 行（`run_start`/`activity`/`heartbeat`/`model_wait`/`resumed`/
`pi_idle`/`pi_idle_wait`/`pi_idle_term`/`pi_idle_kill`/`pi_resumed`/
`run_failed`/`run_end`/`run_stopping`/`run_stopped`）、idle 卡死自动恢复
（Issue #94/#169/#181）和停止顺序的完整字段说明见
[Operations](docs/operations.mdx)。

GitHub（手机，自动更新）：领取任务后，Runner 在 source Issue 上创建一条带隐藏
run marker（`<!-- muyan-pilot:run=<run_id> -->`）的进度评论，之后只 PATCH 同一
条评论（进度变化时立即，且间隔不超过 30 秒），不新增 heartbeat 垃圾评论；关键
milestone（started、plan ready、tests passed/failed、review findings、PR
opened、merged、blocked）单独发布简短评论，让 GitHub Mobile 主动推送通知。
进度评论是纯旁路（Issue #79）：它失败只记 `progress_publish_failed`，绝不改变
交付结果；`Muyan Pilot opened PR:` scene 评论不是旁路（resume 靠它）。

Prometheus / Grafana（可选，只读，独立）：`monitoring/` 下是一套只读、独立、
版本管理的观测栈（Issue #162），Runner 完全不知道它存在；安装与验证步骤见
`monitoring/grafana/README.md`。

## 配置

配置使用 TOML，由人维护，AI 通过 PR 修改。开源仓库只提交 example，真实配置不
提交（`cp .muyan-pilot.example.toml muyan-pilot.toml`）。完整字段表（含
`active_milestone`、`max_concurrency`、`model_wait_dead_seconds`、
`pi_providers`/`pi_provider`/`pi_model`/`pi_thinking` 的模型 provider 配置）见
[Getting started](docs/getting-started.mdx)。

## 并发限制（max_concurrency）

`max_concurrency`（默认 1，只允许 1 或 2）同时决定本机允许的 Pilot 并发任务数
和已启用 timer 的数量（1 或 2）；slot 仍是最终安全边界——拿不到 slot 的 Runner
记录 `capacity_full` 后正常退出，不领取 Issue。slot 是
`<repo_dir>/.muyan-pilot/slots/slot-N` 文件上的排他 `flock(2)` 锁，内核在进程
退出时自动释放，活着的持有者永远不会丢失 slot，死掉的持有者永远不会占住
slot。完整说明见 [Operations](docs/operations.mdx)。

## 任务 base 与 worktree

每次领取任务前，Runner 冻结 `origin/<base_branch>` 的精确 SHA（`base_branch`
在 TOML 中配置，默认 `main`）；任务 worktree 和 feature branch 都从该 SHA
创建，绝不使用主工作区当前 HEAD；branch 和目录名都带唯一 run 标识（例如
`.worktrees/muyan-pilot-orbi-build-orbi-issue-14-a1b2c3d4`），同一个 Issue
返工时会生成新的独立 run，旧现场原样保留。任务 worktree 共享部署 checkout 的
单一 `origin` remote（Git transport 为 SSH，见「Git transport」），worktree
内的 fetch/push（包括 workflow 文件）都走这条 SSH 通道。

Agent 在提交处停止；创建 PR 前的 base 新鲜度是 Runner 的确定性收口（Issue
#186）：Runner 在 base-sync 锁下重新 fetch `origin/<base_branch>`，若已前进则
用 plain `git merge` 合入最新 base（冲突时 abort，PR 在 Agent 的 head 上打开，
由既有审查会话在会话内吸收 base），然后 plain push task branch 并创建 PR。不
自动解决冲突，不 force push，不 merge 或 push 保护分支。

Runner 被 SIGKILL 时任务 worktree 和 `ai-in-progress` 领取标签会留在 GitHub
上；下一个 tick 的领取扫描在 ready 队列之前先扫描 `ai-ready`+`ai-in-progress`
（且未处于其他交付状态）的 open Issue——**仅当没有其他 Runner 活着时**
（flock 锁是唯一事实源）——复用最新 worktree 的 run id（branch、worktree、
进度评论都由它驱动），按隐藏 run marker 找回同一条进度评论继续 PATCH，不新建
run、不新建 worktree、不新建评论。`.worktrees/` 已加入 `.gitignore`。

## 全链路 run_id（correlation ID）

每个任务 attempt 只生成一次 `run_id`（8 位 hex，例如 `e07383c2`），语义等同
trace ID：implement、review、merge 全部复用同一个值；同一个 Issue retry 时生成
新的 run_id。不创建 `trace_id`/`log_id`/另一套 UUID，不引入 tracing backend。

同一个 run_id 出现在：该 attempt 的每条 journal 日志首字段（`[e07383c2]`）；
Issue/PR 评论（可见字段 `run_id=e07383c2` + 隐藏 marker
`<!-- muyan-pilot:run=e07383c2 -->`）；feature branch 与 worktree 名；Pi
session 目录与 run artifacts 的路径；注入 Pi 的 prompt context；PR body 的稳定
machine-readable marker（Runner 验收时校验，缺失即 fail fast，拒绝该 PR）。
缺少合法 run_id 的 run-scoped 事件会 fail fast，不做回退。查询方式（不依赖
内存映射，进程重启后仍可恢复关联）：

```bash
journalctl --user -u 'muyan-pilot@*.service' | grep e07383c2
gh search issues "e07383c2" --repo orbi-build/orbi
```

## 远程 CI（GitHub Actions）

仓库契约（全量 pytest + 分层覆盖率门禁：全仓库 line/branch >= 95% 分别
检查、变更 Python 代码 100%，Issue #234）不只在本机 Runner 上跑：
`.github/workflows/ci.yml` 让 GitHub Actions 在每次 `pull_request` 和每次
`push` 到 `main` 时跑同一份契约，测试失败、line 或 branch 任一层级低于
95%、或变更代码未达 100% 时 CI 变红。单个
job，不加 lint、矩阵或缓存（Issue #56）。生产运行时是 Runner 机器的
`/usr/bin/python3`（3.14.6），GitHub-hosted runner 没有这个解释器，所以
workflow 用 `actions/setup-python` 固定同一 minor 版本 `3.14`，契约命令通过
PATH 上固定的 `python3` 执行。CI 还在干净环境里用 `uv tool install` 安装
console script 并验证入口（`muyan-pilot --help` / `muyan-pilot --version`
退出码 0，Issue #140/#152）。「干净环境安装后 `muyan-pilot --help` 成功」是
永久门禁，不是一次性本地检查。完整说明见 [Testing](docs/testing.mdx)。

## 许可证

本项目以 [Apache License 2.0](LICENSE)（SPDX 标识 `Apache-2.0`）发布，完整
文本见根目录 [LICENSE](LICENSE) 文件。
