# Muyan Pilot

最小 bootstrap：从 `xqliu/muyan-ceo` 领取一个 `ai-ready` Issue，启动 Pi，在隔离 worktree 中完成开发并创建 PR。

## 当前运行

```bash
/usr/bin/python3 bootstrap_runner.py
```

前置条件：

- `gh auth status` 成功；
- `pi --version` 成功；
- 当前目录是一个已初始化并有 remote 的 Git 仓库；
- `prompt.md` 存在。

bootstrap 是一次处理一个 Issue 的临时入口。它成功后由 Pi 自己开发持续 daemon；不在这里提前加入队列、数据库、重试或复杂恢复。
