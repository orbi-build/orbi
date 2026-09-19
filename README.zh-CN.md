[English](README.md) | 简体中文

# Orbi

**GitHub Issues in, tagged releases out.**

Orbi 从 GitHub Issue 领取任务，在隔离 worktree 中开发，运行独立审查会话，只合并经过审查的 head；发布 Issue 冻结 SHA 并发布 tag。GitHub Issue 是唯一状态存储——没有数据库、队列或 daemon。

**公开账本：** [已合并 PR](https://github.com/orbi-build/orbi/pulls?q=is:merged) · [已关闭 Issue](https://github.com/orbi-build/orbi/issues?q=is:closed) · [tagged release](https://github.com/orbi-build/orbi/releases) — 仓库就是记录。

**查看一条完整链路：** [Issue #835](https://github.com/orbi-build/orbi/issues/835) → [PR #843](https://github.com/orbi-build/orbi/pull/843) → [Release v0.5.4](https://github.com/orbi-build/orbi/releases/tag/v0.5.4)

官网 <https://orbi.build> ｜ [Orbi Managed Cloud](https://orbi.build/cloud/?ref=gh-readme) ｜ 文档 <https://docs.orbi.build/> ｜ [Discussions](https://github.com/orbi-build/orbi/discussions)

## 快速开始

```bash
git clone https://github.com/orbi-build/orbi.git && cd orbi
uv tool install --force --reinstall --editable --python python3 .  # 兼容的系统 Python（>= 3.14，如 Fedora 43、当前 Arch）；更老的系统 Python（如 Ubuntu 24.04 自带 3.12）改用 --python 3.14，让 uv 自动供应
```

只想要 CLI 本体？它已发布到 PyPI：[`orbi-cli`](https://pypi.org/project/orbi-cli/)
（要求 Python ≥ 3.14；安装后的命令仍是 `orbi`）：

```bash
uv tool install orbi-cli  # 隔离的 tool 安装；系统 Python 过旧时（如 Ubuntu 24.04 的 3.12）加 --python 3.14，让 uv 自动供应兼容解释器
pip install orbi-cli      # 或在已激活的 Python >= 3.14 环境里
orbi --version            # → orbi <version>
```

卸载用 `uv tool uninstall orbi-cli`。要运行 Orbi 本身（而不仅是 CLI），用
[快速开始](docs/zh/getting-started.mdx)顶部的一键安装——它创建的是 Orbi
部署所驱动的 editable 安装。

### 就绪检查（setup 之前）

- `uv`：`uv --version`；Pi 和其 provider：`pi --version`，然后运行 `pi --print "reply with the single word: ok"`
- GitHub CLI ≥ 2.94（从[官方仓库](https://github.com/cli/cli/blob/trunk/docs/install_linux.md)安装——Ubuntu 24.04 自带的 2.45.0 过旧）：先运行一次 `gh auth login`，再验证 `gh auth status`
- Linux —— systemd user session：`systemctl --user status`
- macOS —— launchd GUI session：`launchctl print gui/$(id -u)`（尚未在真机验证，欢迎回报）

按 [Getting started](docs/zh/getting-started.mdx) 选择模式：自举模式使用本 checkout 作为 `repo_dir`；[External single-repo mode](docs/zh/getting-started.mdx#external-single-repo-mode-deploy_home) 使用本 checkout 作为 `deploy_home`，外部仓库作为 `repo_dir`。

```bash
cp src/orbi/example_config.toml orbi.toml
orbi setup --config orbi.toml  # 4. 一次性 setup（检查既有 gh auth、labels、调度器 units（systemd/launchd）、checkout；幂等）
PYTHONPATH=src python3 -m orbi.runner --config orbi.toml  # 5. 手动跑一个 tick（首次验证；日常由 timer 调度）
orbi doctor --config orbi.toml  # 6. 验证部署健康
```

## 为什么用 Orbi

- **GitHub Issue 就是任务池**：`ai-ready` 标签派活，交付记录（评论、PR、CI）
  天然完整，无需第二套任务系统；
- **全自动运行**：用户级调度 timer（Linux 为 systemd，macOS 为 launchd）每 5 分钟触发一次 tick，正常运行不需要
  status 命令、轮询或督工；
- **独立审查 + 合并门禁**：PR 打开后由独立审查会话审查并在会话内修复，只有被
  审查的 head 能合并，AI 从不 merge 或 push 保护分支；
- **fail fast**：命令错误立即失败并在日志留下现场，Issue 标记 `ai-blocked`
  等待人工决策，不做静默回退；
- **全程可观测**：每条 journal 日志和 GitHub 进度评论都携带同一个 `run_id`，
  一条 grep 即可还原完整时间线。

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
  `install-units` 幂等安装调度器 units（Linux 为 systemd，macOS 为 launchd），`doctor` 只读诊断。

## 文档入口

| 主题 | 入口 |
|---|---|
| 文档首页 | <https://docs.orbi.build/> |
| 新手安装（前提、配置、首次运行、smoke） | [Getting started](docs/zh/getting-started.mdx) |
| 一次性 setup（labels、units、transport 迁移） | [One-time setup](docs/zh/setup.mdx) |
| 工作流（状态链、labels、P0、Epic、Release） | [Workflow](docs/zh/workflow.mdx) |
| 运维（timer、journal、unit drift、恢复） | [Operations](docs/zh/operations.mdx) |
| 测试与覆盖率门禁、远程 CI | [Testing](docs/zh/testing.mdx) |
| 贡献（Issue 粒度、KISS/LEAN、PR 流程） | [Contributing](docs/zh/contributing.mdx) |
| 中文文档 | [docs/zh/](docs/zh/) |

## 开发与贡献

开发契约见 [AGENTS.md](AGENTS.md)；如何派 Issue、报 bug、提 PR 见
[Contributing](docs/zh/contributing.mdx)。runtime 代码在 `src/orbi/`
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
