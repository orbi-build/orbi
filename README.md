# Muyan Pilot

最小 bootstrap：从配置文件中的 source repos 按顺序领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR。

## 当前运行

```bash
/usr/bin/python3 bootstrap_runner.py --config muyan-pilot.toml
```

前置条件：

- `gh auth status` 成功；
- `pi --version` 成功；
- 配置文件存在且包含 `source_repos`；
- 配置中的 repo、workspace、prompt 和 context 路径正确。

配置使用 TOML，由人维护，AI 通过 PR 修改：

```toml
source_repos = ["owner/pilot", "owner/backlog"]
repo_dir = "."
workspace_root = ".."
prompt = "prompt.md"
timeout = 1800
skills = []
context_files = []
```

bootstrap 是一次处理一个 Issue 的临时入口。它成功后由 Pi 自己开发持续 daemon；不在这里提前加入队列、数据库、重试或复杂恢复。
