# Orbi Docker 部署（社区维护，非官方支持）

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
- 任务池仓库名（`owner/repo`）。

## 构建镜像

```bash
git clone https://github.com/orbi-build/orbi.git
cd orbi
docker build -t orbi-docker 3rd/docker
```

镜像内容：`python:3.14-slim` 基础镜像 + Python 3.14 + git + gh CLI +
uv + systemd（user session）+ Node.js + Pi（官方前提条件表中的开发
agent，按 pi.dev 文档以 `npm install -g --ignore-scripts` 安装）。
镜像内**不含任何凭据**。

## 一键启动

```bash
docker run -d --name orbi \
  --stop-signal SIGRTMIN+3 \
  --tmpfs /run --tmpfs /tmp \
  --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v orbi-deploy:/orbi \
  -v orbi-work:/work \
  -e GH_TOKEN="github_pat_xxx" \
  -e ORBI_SOURCE_REPO="OWNER/REPO" \
  -e ORBI_ENV_PROVIDER_API_KEY="sk-xxx" \
  orbi-docker
```

`--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw` 让容器内
systemd（PID 1）能在宿主 cgroup 树上创建自己的 scope——没有它
systemd 无法启动（实证：private cgroupns 下 `/init.scope` 创建失败；
无需 `--privileged`）。

一条命令，不需要 docker compose（Orbi 只有一个 runner 服务）。参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `-e GH_TOKEN=...` | 是 | GitHub token。entrypoint 把它写进 `/orbi/.orbi/env`（systemd `EnvironmentFile` 约定），gh 与 git HTTPS credential helper 在 tick 时从那里读取。不进镜像层 |
| `-e ORBI_SOURCE_REPO=OWNER/REPO` | 是 | 任务池仓库 |
| `-e ORBI_ENV_<NAME>=...` | 否 | 透传任意 provider API key：写入 `/orbi/.orbi/env` 为 `<NAME>=<value>`，供 `pi_providers` 文件以 `$NAME` 引用 |
| `-e ORBI_BASE_BRANCH=main` | 否 | 交付基准分支（默认 `main`） |
| `-v orbi-deploy:/orbi` | 是 | deploy home volume：orbi 源码 checkout、`orbi.toml`、`.orbi/` 状态 |
| `-v orbi-work:/work` | 是 | 交付 checkout volume：任务 worktree 在 `/work/.worktrees/` |
| `-v /sys/fs/cgroup:/sys/fs/cgroup:rw` | 是 | systemd（PID 1）需要可写的 cgroup 子树；缺了它无法启动 |
| `--stop-signal SIGRTMIN+3` | 建议 | systemd 的停机信号；`docker stop -t 30` 给 systemd 足够的收尾时间 |
| `--tmpfs /run --tmpfs /tmp` | 建议 | systemd 期望的可写运行时目录 |

首次启动流程：entrypoint 校验注入变量 → 克隆 orbi 源码到 `/orbi`（已
存在则复用）→ 生成 `/orbi/orbi.toml`（**已存在则绝不覆盖**，可直接
挂载自己的配置）→ 写 `/orbi/.orbi/env` → 克隆任务池到 `/work`（已
存在则复用）→ `gh auth setup-git`（HTTPS transport，git 凭据走 gh
credential helper，全程无 SSH key）→ 官方可编辑安装 `uv tool install
--editable /orbi` → 交给 systemd → oneshot 单元拉起 user manager 并
执行官方 `orbi setup`（幂等，每次启动都会重跑）→ timer 接管，每
5 分钟一个 tick。

生成的 `orbi.toml`：

```toml
source_repos = ["OWNER/REPO"]
repo_dir = "/work"
deploy_home = "/orbi"
workspace_root = "/work"
base_branch = "main"
git_transport = "https"
engine_source_track = "release"
max_concurrency = 1
```

## 状态持久化

全部状态在两个 volume 里，容器重建不丢：

| 路径 | 内容 |
|---|---|
| `/orbi` | orbi 源码 checkout（`engine_source_track = "release"` 自动跟随最新发布）、`orbi.toml`、`.orbi/env`（凭据）、`.orbi/` 运行时状态 |
| `/work` | 任务池交付 checkout、任务 worktree（`/work/.worktrees/`）、slot 锁（`/work/.orbi/slots/`） |

验证方式：`docker stop` → `docker rm` → 用**同样的 volume 和变量**
再跑一次 `docker run`：`orbi setup` 幂等重跑（`cli=verified`），
`.orbi/` 状态与 worktree 原样保留，timer 继续工作。容器重启打断的
in-flight run 与宿主机断电重启语义相同：下一个 tick 的 restart-resume
扫描自动接续。

bind mount 注意：如果挂载宿主机已有 checkout（如
`-v ~/projects/myrepo:/work`），宿主机 uid 需与容器内 `orbi` 用户一致
（默认 1000），否则 git 会报 dubious ownership；建议使用 named volume。

## 配置模型 provider（跑真实任务）

镜像自带 Pi。让 runner 真正派活需要可用的模型端点
（[providers 文档](https://docs.orbi.build/providers)）。最简单的
Orbi 原生路径（Path B）：

1. 启动时注入 `-e ORBI_ENV_PROVIDER_API_KEY="sk-xxx"`（或任意
   `ORBI_ENV_GROQ_API_KEY` 等名字）；
2. 编辑 `/orbi/.orbi/pi-providers.json`（`orbi setup` 已生成 starter，
   `apiKey` 写 `"$PROVIDER_API_KEY"` 这样的环境变量引用）；
3. 在 `/orbi/orbi.toml` 里加 `pi_provider` / `pi_model` / `pi_providers
   = ".orbi/pi-providers.json"`，重启容器（`docker restart orbi`）。

派活前先验证端点（provider/model 与 `pi-providers.json` 中一致，示例是
scaffold 的 openai 端点；返回真实 API 回复即端点与 key 可用，401/超时
就是 key 或网络问题）：

```bash
docker exec -u orbi orbi bash -c '. /orbi/.orbi/env && pi --provider openai \
  --model gpt-4o-mini --api-key "$PROVIDER_API_KEY" \
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

升级：`docker build` 出新镜像后按上面的命令重建容器即可——配置与
状态都在 volume 里，entrypoint 与 `orbi setup` 幂等收敛。

## 故障与修复

| 现象 | 原因与修复 |
|---|---|
| 容器立即退出，`docker logs` 显示 `GH_TOKEN is required` / `ORBI_SOURCE_REPO is required` | 缺少注入变量。按提示补 `-e` 参数重建容器 |
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
