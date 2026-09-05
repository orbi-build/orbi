# Orbi

Orbi 是一个本地 AI 开发 Worker：把任务放进 GitHub Issue，它自动领取，启动
Pi 在隔离 worktree 中完成开发、测试并创建 PR，再经过独立审查与合并门禁后
合入。GitHub Issue 与标签是唯一状态存储——没有数据库、队列或 daemon。

这个仓库自己就是证据：最近 106 个已合并 PR 里，99 个是 Orbi 自己交付的。

- 官网 <https://orbi.build> ｜ 文档 <https://docs.orbi.build/>（仓库内
  [`docs/`](docs/) 是唯一事实源，中文入口 [`docs/zh/`](docs/zh/)）｜ 进展
  [@xqliu](https://x.com/xqliu)
- **[报名首批共建](https://orbi.build/apply)**：卡在环境、模型接入或工作流上的
  话，我们帮你跑通第一个 Issue，你踩的坑会变成优先修的 Issue。

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
# 2. 安装 CLI（editable uv tool 安装，从 orbi 仓库安装一次；两种部署模式共用）
uv tool install --force --reinstall --editable --python /usr/bin/python3 .
# 3. 创建配置（仓库只提交 example，真实配置本地维护）
cp .orbi.example.toml orbi.toml
# 4. 一次性 setup（gh auth、labels、systemd units、checkout 检查；幂等）
orbi setup --config orbi.toml
# 5. 手动跑一个 tick（首次验证；日常由 timer 调度）
PYTHONPATH=src python3 -m orbi.runner --config orbi.toml
# 6. 验证部署健康
orbi doctor --config orbi.toml
```

完整前提（Python 3.14、Pi、git + gh、systemd、模型端点）与从 0 开始的 smoke
walkthrough 见 [Getting started](docs/getting-started.mdx)；setup 的完整输出
契约与失败现场见 [One-time setup](docs/setup.mdx)。

两种部署模式（Issue #330）：**自举模式**（默认，`repo_dir` 就是 orbi
checkout 自身）与**外部单仓库模式**（`deploy_home` 指向 orbi checkout、
`repo_dir` 指向用户仓库 X——orbi 安装一次后指向任意用户仓库，X 里开
`ai-ready` issue 即被开发）。配置差异见 [Getting started](docs/getting-started.mdx)
的 External single-repo mode 一节。

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
- 支持 `orbi add` 派活、`status` 查看队列、`session` 跟随 Pi 会话，
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
[Contributing](docs/contributing.mdx)。runtime 代码在 `src/orbi/`
package（Issue #168 src layout，editable finder 映射整个 package 目录，
新增模块无需重装）；checkout 根目录没有 `orbi.py`（避免遮蔽已安装
package），直接执行兼容入口是 `python3 -m orbi.cli`，不是正式使用
方式。

## 许可证

本项目是 [fair-code](https://faircode.io)，以 **Sustainable Use License**
（v1.0）发布，完整文本见根目录 [LICENSE.md](LICENSE.md)。

实际含义：

- **在自己的仓库上跑 Orbi 永久免费** —— 个人用、公司内部用都一样，不限规模，
  改代码、自托管、跑一千个仓库都不需要向我们申请授权。
- **可以分享**，前提是免费且用于非商业目的。
- **只有把 Orbi 本身卖出去才需要商业授权** —— 即托管成服务卖给你的客户，
  或嵌入你收费的产品里。

不确定自己的用法算哪一边，来
[Discussions](https://github.com/orbi-build/orbi/discussions) 问，我们直说。
