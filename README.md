# Orbi

Orbi 是一个本地 AI 开发 Worker：把任务放进 GitHub Issue，它自动领取，启动
Pi 在隔离 worktree 中完成开发、测试并创建 PR，再经过独立审查与合并门禁后
合入。GitHub Issue 与标签是唯一状态存储——没有数据库、队列或 daemon。

- 官网：<https://orbi.build>
- 文档站：<https://docs.orbi.build/>（仓库内 [`docs/`](docs/) 是唯一事实源；
  中文入口 [`docs/zh/`](docs/zh/)）

## 为什么用 Orbi

- **GitHub Issue 就是任务池**：`ai-ready` 标签派活，交付记录（评论、PR、CI）
  天然完整，无需第二套任务系统；
- **全自动运行**：systemd user timer 每 5 分钟触发一次 tick，正常运行不需要
  status 命令、轮询或督工；
- **独立审查 + 合并门禁**：PR 打开后由独立审查会话审查并在会话内修复，只有被
  审查的 head 能合并，AI 从不 merge 或 push 保护分支；
- **fail fast**：命令错误立即失败并在日志留下现场，Issue 标记 `ai-blocked`
  等待人工决策，不做静默回退；
- **全程可观测**：每条 journal 日志和 GitHub 进度评论都携带同一个 `run_id`，
  一条 grep 即可还原完整时间线。

## 快速开始

```bash
# 1. clone
git clone https://github.com/orbi-build/orbi.git && cd orbi
# 2. 安装 CLI（editable uv tool 安装，官方本地部署方式）
uv tool install --force --reinstall --editable --python /usr/bin/python3 .
# 3. 创建配置（仓库只提交 example，真实配置本地维护）
cp .muyan-pilot.example.toml muyan-pilot.toml
# 4. 一次性 setup（gh auth、labels、systemd units、checkout 检查；幂等）
muyan-pilot setup --config muyan-pilot.toml
# 5. 手动跑一个 tick（首次验证；日常由 timer 调度）
python3 bootstrap_runner.py --config muyan-pilot.toml
# 6. 验证部署健康
muyan-pilot doctor --config muyan-pilot.toml
```

完整前提（Python 3.14、Pi、git + gh、systemd、模型端点）与从 0 开始的 smoke
walkthrough 见 [Getting started](docs/getting-started.mdx)；setup 的完整输出
契约与失败现场见 [One-time setup](docs/setup.mdx)。

## 它能做什么

```text
GitHub Issue（ai-ready）
  → 领取：建 feature branch + 隔离 worktree（从冻结的 origin/main SHA）
  → Pi 开发：plan → implement → test → verify
  → commit 交付（Agent 在提交处停止）
  → Runner 收口：同步最新 base、push、创建 PR（body 带 Fixes #N）
  → 独立审查（会话内修复）→ 合并门禁 → merge
```

- 每个任务一个独立 run：branch、worktree、日志、PR 全部用同一个 `run_id`
  关联，重试生成新 run，旧现场原样保留；
- 失败分类明确：可恢复失败回到同一 PR 继续修复，不可恢复失败标记
  `ai-blocked` 交给人；
- 支持 `muyan-pilot add` 派活、`status` 查看队列、`session` 跟随 Pi 会话，
  `install-units` 幂等安装 systemd units，`doctor` 只读诊断。

## 文档入口

| 主题 | 入口 |
|---|---|
| 文档首页 | <https://docs.orbi.build/> |
| 新手安装（前提、配置、首次运行、smoke） | [Getting started](docs/getting-started.mdx) |
| 一次性 setup（labels、units、transport 迁移） | [One-time setup](docs/setup.mdx) |
| 工作流（状态链、labels、P0、Epic、Release） | [Workflow](docs/workflow.mdx) |
| 运维（timer、journal、unit drift、恢复） | [Operations](docs/operations.mdx) |
| 测试与覆盖率门禁、远程 CI | [Testing](docs/testing.mdx) |
| 贡献（Issue 粒度、KISS/LEAN、PR 流程） | [Contributing](docs/contributing.mdx) |
| 中文文档 | [docs/zh/](docs/zh/) |

## 开发与贡献

开发契约见 [AGENTS.md](AGENTS.md)；如何派 Issue、报 bug、提 PR 见
[Contributing](docs/contributing.mdx)。`muyan_pilot.py` 的直接执行入口保留为
开发/兼容路径，不是正式使用方式。

## 许可证

本项目以 [Apache License 2.0](LICENSE)（SPDX 标识 `Apache-2.0`）发布，完整
文本见根目录 [LICENSE](LICENSE) 文件。
