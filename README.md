English | [简体中文](README.zh-CN.md)

# Orbi

Orbi is a local AI development Worker: put work in a GitHub Issue, and it automatically claims the Issue, starts Pi in an isolated worktree to develop and test it, creates a PR, and then passes it through independent review and merge gates. GitHub Issues and labels are the only state store—there is no database, queue, or daemon.

This repository is its own evidence: 99 of the most recent 106 merged PRs were delivered by Orbi itself.

- Website <https://orbi.build> ｜ Documentation <https://docs.orbi.build/> (the repository's [`docs/`](docs/) is the single source of truth; the Chinese entry point is [`docs/zh/`](docs/zh/)) ｜ Updates [@xqliu](https://x.com/xqliu)
- **[Join the first group of contributors](https://orbi.build/apply)**: if you are stuck on the environment, model integration, or workflow, we can help you run your first Issue. The problems you encounter become Issues to prioritize.

## Why Orbi

- **GitHub Issues are the task pool**: the `ai-ready` label dispatches work, and the delivery record (comments, PRs, and CI) is complete by default, with no second task system;
- **Fully automated**: a systemd user timer triggers a tick every 5 minutes. Normal operation needs no status command, polling, or supervision;
- **Independent review + merge gates**: after a PR opens, an independent review session reviews it and fixes findings in the same session. Only the reviewed head can merge, and AI never merges or pushes protected branches;
- **Fail fast**: command errors fail immediately and leave the evidence in the logs. The Issue is marked `ai-blocked` for a human decision, with no silent fallback;
- **Observable end to end**: every journal log and GitHub progress comment carries the same `run_id`, so the complete timeline can be reconstructed with one grep.

## Quick start

```bash
# 1. clone
git clone https://github.com/orbi-build/orbi.git && cd orbi
# 2. install the CLI (editable uv tool installation; install once from the Orbi repository; shared by both deployment modes)
uv tool install --force --reinstall --editable --python /usr/bin/python3 .
# 3. create configuration (only the example is committed; maintain the real configuration locally)
cp .orbi.example.toml orbi.toml
# 4. run one-time setup (checks prior gh auth, labels, systemd units, and checkout; idempotent)
orbi setup --config orbi.toml
# 5. manually run one tick (for initial verification; the timer schedules normal runs)
PYTHONPATH=src python3 -m orbi.runner --config orbi.toml
# 6. verify deployment health
orbi doctor --config orbi.toml
```

### Ready check (before setup)

- `uv` installed: `uv --version`
- Pi installed and a model provider works: `pi --version`, then
  `pi --print "reply with the single word: ok"`
- GitHub CLI authenticated: run `gh auth login` once, then verify with
  `gh auth status`
- A systemd user session is available: `systemctl --user status`

Choose the configuration mode in [Getting started](docs/getting-started.mdx):
**bootstrap mode** uses this checkout as `repo_dir`; [External single-repo
mode](docs/getting-started.mdx#external-single-repo-mode-deploy_home) uses
this checkout as `deploy_home` and a foreign user repository as `repo_dir`.
See [One-time setup](docs/setup.mdx) for the setup output contract.

## What it does

```text
GitHub Issue (ai-ready)
  → Claim: create a feature branch + isolated worktree (from the frozen origin/main SHA)
  → Pi development: plan → implement → test → verify
  → Commit delivery (the Agent stops at the commit)
  → Runner closeout: sync the latest base, push, and create a PR (body includes Fixes #N)
  → Independent review (fixes in the same session) → merge gate → merge
```

- Each task gets its own run: the branch, worktree, logs, and PR are all associated with the same `run_id`; retries create a new run and preserve the old evidence unchanged;
- Failures are classified clearly: recoverable failures return to the same PR for continued fixes, while unrecoverable failures mark the Issue `ai-blocked` for a human;
- Supports `orbi add` for dispatching work, `status` for viewing the queue, `session` for following the Pi session, `install-units` for idempotently installing systemd units, and `doctor` for read-only diagnostics.

## Documentation

| Topic | Entry point |
|---|---|
| Documentation home | <https://docs.orbi.build/> |
| Getting started (prerequisites, configuration, first run, smoke test) | [Getting started](docs/getting-started.mdx) |
| One-time setup (labels, units, transport migration) | [One-time setup](docs/setup.mdx) |
| Workflow (state chain, labels, P0, Epic, Release) | [Workflow](docs/workflow.mdx) |
| Operations (timer, journal, unit drift, recovery) | [Operations](docs/operations.mdx) |
| Testing, coverage gates, and remote CI | [Testing](docs/testing.mdx) |
| Contributing (Issue granularity, KISS/LEAN, PR flow) | [Contributing](docs/contributing.mdx) |
| Chinese documentation | [docs/zh/](docs/zh/) |

## Development and contribution

See the development contract in [AGENTS.md](AGENTS.md), and [Contributing](docs/contributing.mdx) for dispatching Issues, reporting bugs, and submitting PRs. Runtime code lives in the `src/orbi/` package (Issue #168 src layout; the editable finder maps the entire package directory, so new modules need no reinstall). The checkout root has no `orbi.py` (to avoid shadowing the installed package); the direct-execution compatibility entry point is `python3 -m orbi.cli`, not the formal usage path.

## License

This project is [fair-code](https://faircode.io), released under the **Sustainable Use License** (v1.0). See the complete text in [LICENSE.md](LICENSE.md) at the repository root.

In practice:

- **Run Orbi on your own repositories for free forever**—for personal use and internal company use alike, at any scale. You can modify the code, self-host it, and run it across a thousand repositories without requesting authorization.
- **You may share it**, provided that it is free and used for non-commercial purposes.
- **Commercial authorization is required only when you sell Orbi itself**—for example, hosting it as a service for customers or embedding it in a paid product.

If you are unsure which side your use falls on, ask in [Discussions](https://github.com/orbi-build/orbi/discussions); we will give you a clear answer.
