# Orbi Docker 部署（社区维护，非官方支持）

[English](README.md) | 简体中文

> **Community-maintained / unofficial.** 本目录（`3rd/docker/`）是 Orbi
> 的 Docker 薄封装外挂，由社区维护，**不属于官方支持范围**。核心部署
> 路径仍是宿主机 systemd user unit/timer（见
> [docs/getting-started](https://docs.orbi.build/getting-started)）；
> 两者**二选一**，不要在同一台机器上并存管理同一个任务池。
> 遇到问题请先自行排查；官方 Issue 优先处理核心路径。

Linux 上一条 `docker run` 启动一个 Orbi runner：容器里跑的是**未改动的
官方部署流程**——镜像内以 systemd 作为 PID 1，并为 `orbi` 用户拉起真实的
user manager，官方 `systemd/orbi@.service` + `orbi@.timer` 模板原样安装、
`orbi setup` 原样执行，`orbi@1.timer` 每 5 分钟触发一个 runner tick。
认证经环境变量注入，全部状态落在两个 volume 里，容器本身无状态，
停止/重建不丢数据。

## 前置条件

- Linux 宿主机上的 Docker（cgroup v2）；
- 一个有任务池仓库写权限的 GitHub token（classic token 带 `repo`
  scope，或 fine-grained token 带 Issues/PR/Contents 读写）；
- 任务池仓库名（`owner/repo`）；
- 真实派活还需一个模型端点 API key（快速开始用 DeepSeek，任何
  OpenAI 兼容端点均可）。

## 快速开始

拉取发布镜像。两个仓库源携带相同 tag：`latest` 和去掉 `v` 前缀的
发布号（如 `0.5.17`）。

```bash
docker pull ghcr.io/orbi-build/orbi:latest
# Docker Hub 上的同一镜像：
docker pull docker.io/orbibuild/orbi:latest
```

一条命令启动 runner——token、任务池和模型 provider 全部来自环境
变量；entrypoint 在首次启动时把它们写进 deploy home 的 env 文件、
生成的 `orbi.toml` 和生成的 `pi-providers.json`（已存在的文件绝不
覆盖）。替换三个带引号的值——token、repo、key；DeepSeek 端点只是
示例，任何 OpenAI 兼容端点都用同样四个变量（`ORBI_PI_PROVIDER`、
`ORBI_PI_MODEL`、`ORBI_PI_BASE_URL`、`ORBI_PI_API_KEY`）：

```bash
# 可选模型上限及默认值——仅在需要覆盖时，把对应的 -e 行加进下面的
# 命令（放在镜像引用之前）：
#   -e ORBI_PI_API=openai-completions
#   -e ORBI_PI_CONTEXT_WINDOW=128000
#   -e ORBI_PI_MAX_TOKENS=16384
docker run -d --name orbi \
  --stop-signal SIGRTMIN+3 \
  --tmpfs /run --tmpfs /tmp \
  --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v orbi-deploy:/orbi \
  -v orbi-work:/work \
  -e GH_TOKEN="github_pat_xxx" \
  -e ORBI_SOURCE_REPO="OWNER/REPO" \
  -e ORBI_PI_PROVIDER=deepseek \
  -e ORBI_PI_MODEL=deepseek-chat \
  -e ORBI_PI_BASE_URL=https://api.deepseek.com \
  -e ORBI_PI_API_KEY="sk-xxx" \
  ghcr.io/orbi-build/orbi:latest
```

`--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw` 让容器内
systemd（PID 1）能在宿主 cgroup 树上创建自己的 scope——没有它
systemd 无法启动（实证：private cgroupns 下 `/init.scope` 创建失败；
无需 `--privileged`）。

一条命令，不需要 docker compose（Orbi 只有一个 runner 服务）。
Docker 层面的参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `-v orbi-deploy:/orbi` | 是 | deploy home volume（里面放什么见下方配置表） |
| `-v orbi-work:/work` | 是 | 交付 checkout volume（里面放什么见下方配置表） |
| `-v /sys/fs/cgroup:/sys/fs/cgroup:rw` | 是 | systemd（PID 1）需要可写的 cgroup 子树；缺了它无法启动 |
| `--stop-signal SIGRTMIN+3` | 建议 | systemd 的停机信号；`docker stop -t 30` 给 systemd 足够的收尾时间 |
| `--tmpfs /run --tmpfs /tmp` | 建议 | systemd 期望的可写运行时目录 |

## 配置如何到达 runner

| 你提供 | 到达 runner 的路径 |
|---|---|
| `ORBI_SOURCE_REPO` | 任务池仓库。首次启动时 entrypoint 把它克隆进 `/work` volume 并写进生成的 `orbi.toml`。替代方案：bind mount 已有 checkout（`-v ~/projects/myrepo:/work`）——宿主机 uid 需与容器内 `orbi` 用户一致（uid 1000），否则 git 报 dubious ownership；named volume 无此问题。 |
| `GH_TOKEN` | entrypoint 把它写进 `/orbi/.orbi/env`（systemd `EnvironmentFile` 约定，权限 600），每次启动都从注入变量重新生成。tick 时 gh 从那里读取，git 的凭据由 gh credential helper 经 HTTPS 提供——全程无 SSH key；token 绝不进镜像层。它需要对**任务池仓库的写权限**：classic token 带 `repo` scope，或 fine-grained token 带 Issues、Pull requests、Contents 读写（runner 要用它建 label、分支和 PR）。 |
| `ORBI_PI_PROVIDER`、`ORBI_PI_MODEL`、`ORBI_PI_BASE_URL`、`ORBI_PI_API_KEY` | 四个模型 provider 值（要么全设，要么全不设——只设一部分会启动失败并点名缺失的变量）。首次启动时 entrypoint 把 `PI_API_KEY` 写进 `/orbi/.orbi/env`，生成 `/orbi/.orbi/pi-providers.json`（一个 provider、一个 model，`apiKey` 引用 `$PI_API_KEY`，key 本身不落 JSON）；生成的 `orbi.toml` 带上 `pi_provider`、`pi_model`、`pi_providers`。 |
| `ORBI_PI_API`、`ORBI_PI_CONTEXT_WINDOW`、`ORBI_PI_MAX_TOKENS` | 生成的 `pi-providers.json` 的可选模型上限；默认 `openai-completions`、`128000`、`16384`。 |
| `ORBI_BASE_BRANCH` | 可选交付基准分支（默认 `main`）。 |
| `ORBI_ENV_<NAME>` | 可选、可重复：透传到 `/orbi/.orbi/env` 为 `<NAME>=<value>`（如 `ORBI_ENV_GROQ_API_KEY` 落地为 `GROQ_API_KEY`），供 provider 文件引用——见"进阶：多 provider"。 |
| `orbi-deploy` volume → `/orbi` | deploy home volume：orbi 源码 checkout（`engine_source_track = "release"` 自动跟随最新发布）、生成的 `orbi.toml`、`.orbi/env` 凭据文件与 `.orbi/` 运行时状态。 |
| `orbi-work` volume → `/work` | 交付 checkout volume：任务池克隆、`/work/.worktrees/` 下的任务 worktree 与 `/work/.orbi/` 下的 slot 锁。 |

首次启动流程：entrypoint 校验注入变量 → 克隆 orbi 源码到 `/orbi`（已
存在则复用）→ 生成 `/orbi/orbi.toml`（**已存在则绝不覆盖**，可直接
挂载自己的配置）→ 写 `/orbi/.orbi/env` → 克隆任务池到 `/work`（已
存在则复用）→ `gh auth setup-git`（HTTPS transport，git 凭据走 gh
credential helper，全程无 SSH key）→ 官方可编辑安装 `uv tool install
--editable /orbi` → 交给 systemd → oneshot 单元拉起 user manager 并
执行官方 `orbi setup`（幂等，每次启动都会重跑）→ timer 接管，每
5 分钟一个 tick。

生成的 `orbi.toml`（带快速开始变量时）：

```toml
source_repos = ["OWNER/REPO"]
repo_dir = "/work"
deploy_home = "/orbi"
workspace_root = "/work"
base_branch = "main"
git_transport = "https"
engine_source_track = "release"
max_concurrency = 1
pi_providers = ".orbi/pi-providers.json"
pi_provider = "deepseek"
pi_model = "deepseek-chat"
```

## 第一次交付

1. 在任务池仓库开一个 Issue，用 `when X, should Y, actually Z` 的形状
   陈述一个可观测的运行时结果。
2. 打上 `ai-ready` label（首次启动已创建十二个平台 label）。
3. 跟踪 tick 日志：

```bash
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  journalctl --user -u orbi@1.service -f
```

两个 tick 内（timer 每 5 分钟触发一次）Issue 会走完
`ai-ready` → `ai-in-progress` → `ai-pr-opened` 并开出 PR；独立评审
一轮后合并（`ai-merged`）。整条路径不需要任何 `docker exec` 配置
步骤——哪一步需要手工 exec，就是文档或 entrypoint 的缺陷。

## Orbi 快速指南

交付所需的基本概念集中在一页；完整参考见
[workflow 文档](https://docs.orbi.build/zh/workflow)。

- **任务长什么样** — 一个 Issue 用 `when X, should Y, actually Z` 的
  形状陈述一个可观测的运行时结果，附一小份验收清单：用户旅程按
  前置条件 → 命令/配置 → 用户看到的成功结果 → 失败路径与修复展开。
- **label 含义** — `ai-ready` 是执行开关：runner 只认领带这个 label
  的 Issue（认领顺序：`p0` 优先，其次 `bug`，再普通 Issue）。被认领
  后走 `ai-in-progress` → `ai-pr-opened` → `ai-merged`（终态；PR 描述
  里的 `Fixes #N` 会自动关闭 Issue）或 `ai-fix-needed`（下一轮评审
  修复发现的问题）；`ai-blocked`（终态）需要人来决策。`ai-epic` 标记
  纯协调 Issue，runner 永不认领；`ai-release` 标记发布任务（见下）。
  交付状态由 runner 管理；`ai-ready`、`ai-epic`、`ai-release`、
  `p0`、`bug` 由你来打和摘。
- **怎么看进度** — 每次运行在 Issue 上只保留一条进度评论，每个
  里程碑原位更新；tick 日志用上面的 `journalctl` 命令实时跟踪。
- **预期结果** — 两个 tick 内出现分支和 PR（描述带 `Fixes #N`），
  随后独立评审会话运行（最多 5 轮），合并落在被评审的那个
  commit 上——之后 GitHub 自动关闭 Issue。
- **如何发布** — 发布是独立的 Issue：打 `ai-ready` + `ai-release`
  两个 label，正文声明 `## Release` 块：`version`、`base_branch`，
  以及 `scope`（`#N` 列表）或 `scope_from_milestone`。Runner 的发布
  状态机冻结 base、等待 CI 全绿、升版本号、打 tag、发布 GitHub
  Release 并关闭对应 Milestone——全程没有 PR；任何失败停在
  `ai-blocked`，由人决策。

## 状态持久化

全部状态都在上方配置表描述的两个 volume 里，容器重建不丢。

验证方式：`docker stop` → `docker rm` → 用**同样的 volume 和变量**
再跑一次 `docker run`：`orbi setup` 幂等重跑（`cli=verified`），
`.orbi/` 状态与 worktree 原样保留，timer 继续工作。容器重启打断的
in-flight run 与宿主机断电重启语义相同：下一个 tick 的 restart-resume
扫描自动接续。

## 从源码构建

需要改动封装本身（entrypoint、setup oneshot、Dockerfile）时，从仓库
checkout 构建镜像：

```bash
git clone https://github.com/orbi-build/orbi.git
cd orbi
docker build -t orbi-docker 3rd/docker
```

镜像内容：`python:3.14-slim` 基础镜像 + Python 3.14 + git + gh CLI +
uv + systemd（user session）+ Node.js + Pi（官方前提条件表中的开发
agent，按 pi.dev 文档以 `npm install -g --ignore-scripts` 安装）。
镜像内**不含任何凭据**。

然后按快速开始同样的 `docker run` 运行，镜像名换成 `orbi-docker`。

## 进阶：多 provider

快速开始从环境变量配置**一个** provider。要同时服务多个
（如 deepseek 加 groq）：

1. 启动时用 `ORBI_ENV_<NAME>`（可重复）注入额外的 key：
   `-e ORBI_ENV_GROQ_API_KEY="gsk-xxx"` 落进 `/orbi/.orbi/env` 为
   `GROQ_API_KEY`；
2. 编辑 `/orbi/.orbi/pi-providers.json`——entrypoint 首次启动生成了
   单 provider 版本且绝不覆盖已有文件——加入其余 provider，每个
   `apiKey` 引用自己的环境变量（`"$GROQ_API_KEY"`）；
3. 把 `/orbi/orbi.toml` 里的 `pi_provider` / `pi_model` 指向要用的
   provider 与 model，然后重启容器：`docker restart orbi`（两个文件
   都是你的——entrypoint 绝不覆盖已有配置）。

首次启动完全没有 provider 变量时不会生成 `pi-providers.json`；
`orbi setup` 会生成 starter 文件供填写。依赖某个端点前先验证
（provider/model 与 `pi-providers.json` 一致；返回真实 API 回复即
端点与 key 可用，401/超时就是 key 或网络问题）：

```bash
docker exec -u orbi orbi bash -c '. /orbi/.orbi/env && pi --provider deepseek \
  --model deepseek-chat --api-key "$PI_API_KEY" \
  --print "reply with the single word: ok"'
```

## 日常操作

容器内命令一律以 `-u orbi` 执行（runner 就是以该用户跑的）；
`systemctl --user` / `journalctl --user` 依赖用户会话总线，`docker exec`
不会自动带上，需要 `-e XDG_RUNTIME_DIR=/run/user/1000`。orbi CLI 装在
`/home/orbi/.local/bin/`（不在 root 的 PATH 里）。

```bash
docker logs -f orbi                                   # setup 输出 + systemd 控制台
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  journalctl --user -u orbi@1.service -n 50 --no-pager  # tick 日志
docker exec -u orbi -w /orbi orbi /home/orbi/.local/bin/orbi status  # 队列与当前任务
docker exec -u orbi -w /orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  /home/orbi/.local/bin/orbi doctor                   # 部署体检
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  systemctl --user list-timers                        # 下次触发时间
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  systemctl --user start orbi@1.service               # 立即触发一个 tick
docker stop -t 30 orbi && docker rm orbi              # 停止并删除（volume 保留）
docker volume rm orbi-deploy orbi-work                # 彻底清除全部状态
```

升级：`docker pull` 新 tag 后按上面的命令重建容器即可（自建镜像则
重新 `docker build` 后同样重建）——配置与状态都在 volume 里，
entrypoint 与 `orbi setup` 幂等收敛。

## 故障与修复

| 现象 | 原因与修复 |
|---|---|
| 容器立即退出，`docker logs` 显示 `GH_TOKEN is required` / `ORBI_SOURCE_REPO is required` | 缺少注入变量。按快速开始补 `-e` 参数重建容器 |
| `partial Pi provider configuration; missing ...` | 四个模型 provider 变量只设了一部分。全设或全不设（见上方配置表），再重建容器 |
| `invalid ORBI_SOURCE_REPO ...` | 值不是 `owner/repo` 形式 |
| `cloning ... failed` | token 无该仓库读权限，或网络不通。修好后重建 |
| setup 显示 `repo=... permission=READ` 或权限错误 | token 没有任务池写权限（需要能建 Issue label / 分支 / PR）。换 token 重建 |
| `orbi setup` 失败（`setup_failed reason=...`） | 按输出里的 reason 修复（多为 token 权限或 transport），`docker restart orbi` 重跑幂等 setup |
| tick 日志 `transport_unreachable` | HTTPS 凭据失效：确认 `GH_TOKEN` 仍有效后重建容器（entrypoint 会重写 env 文件） |
| tick 日志 `unit_drift` 后自动 `auto_synced` | 正常自愈：官方模板更新后下一个 tick 自动重装，无需操作 |
| `docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi systemctl --user is-active` 失败 | 容器内 user manager 异常，`docker restart orbi`；反复出现请到仓库提 Issue（注明 docker 外挂、非官方） |

## 设计说明（为什么不重写调度）

Orbi 的执行模型依赖 `systemctl --user` timer 调度：`orbi setup` 启动
前探测 user bus，tick 前的 unit-drift 自愈要跑 `systemctl --user
daemon-reload`。因此容器内不是用 cron/supervisor 复刻一个调度器，而是
直接跑 systemd（PID 1）+ `user@1000.service` 真实 user manager——
官方模板、`orbi setup`、timer、自愈全部原样工作，本目录只做薄封装：
准备 volume、注入凭据、装 CLI，然后交给官方流程。

## 与核心 systemd 部署的关系

**二选一。** Docker 外挂面向"没有 user systemd 的平台/环境快速部署"
（NAS、云主机容器平台等）；有 systemd user session 的 Linux 主机请走
[官方部署路径](https://docs.orbi.build/getting-started)。不要让两条
路径指向同一个任务池仓库（双 runner 会互相抢票）。
