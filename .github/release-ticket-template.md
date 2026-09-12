## 背景

vX.Y.Z 收哪些票、为什么这样切版。

## 不属于本版

明确列出留在其他里程碑的票，避免混入。

## 前置条件

- milestone vX.Y.Z 除本票外全部关闭
- release commit CI 全绿

任一前置未满足时按 #381 进入可恢复等待，不烧成终态 `ai-blocked`。

**本票带 `ai-ready`**：开票即应可被认领，否则是死票。

## 验收

- 远端存在 tag `vX.Y.Z` 与对应 GitHub Release
- Release notes 包含本版范围内的票号
- 范围内各票状态为 `ai-merged`

## Release

- version: vX.Y.Z
- base_branch: main
- scope_from_milestone: vX.Y.Z

## Release 段备忘（仅供参考：生效契约只有上一段，开票时删除本节）

- `version` 是精确 tag 名、`base_branch` 是 release commit 冻结自的分支，两者不含空格、不得为空。
- `scope`（手列）与 `scope_from_milestone`（Milestone 标题，不含空格）**恰好二选一**，两个都写或都不写都会被解析器拒绝。
- 手列 `scope` 的写法（列表项为 `#N`，至少一项）：

```markdown
## Release

- version: vX.Y.Z
- base_branch: main
- scope:
  - #123
  - #124
```

- 非 Python 项目必须显式 `version_file`（缺省按 `pyproject.toml` 改；orbi-build/orbi-cloud#246 因漏写卡住）：

```markdown
## Release

- version: vX.Y.Z
- base_branch: main
- scope_from_milestone: vX.Y.Z
- version_file: package.json
```

- `version_file` 支持全集：`pyproject.toml`（默认）、`package.json`、`pom.xml`、`build.gradle`、`build.gradle.kts`、`gradle.properties`、`Cargo.toml`、`composer.json`、`pubspec.yaml`、`none`。
- legacy `test_command`：#569 起接受但忽略，永不执行；测试判据是 release commit 上的 GitHub Actions CI 结果（#268 CI-wait 门禁）。
