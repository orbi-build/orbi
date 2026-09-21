English | [简体中文](README.zh-CN.md)

# Orbi

**GitHub Issues in, tagged releases out.**

Orbi claims an Issue from GitHub Issues, develops it in an isolated worktree, runs an independent review session, merges only the reviewed head, and lets a release Issue freeze the SHA and publish the tag. GitHub Issues are the only state store—no database, queue, or daemon.

**Ledger:** [merged PRs](https://github.com/orbi-build/orbi/pulls?q=is:merged) · [closed Issues](https://github.com/orbi-build/orbi/issues?q=is:closed) · [tagged releases](https://github.com/orbi-build/orbi/releases) — the repository is the record.

**Inspect one loop:** [Issue #1018](https://github.com/orbi-build/orbi/issues/1018) → [PR #1023](https://github.com/orbi-build/orbi/pull/1023) → [Release v0.5.17](https://github.com/orbi-build/orbi/releases/tag/v0.5.17)

## The loop, in 35 seconds

That same loop, recorded from the live GitHub pages — including the review that rejected the first attempt:

<video src="https://github.com/user-attachments/assets/1f0d3a62-5fc9-44ae-bf68-b312a6a55f85" controls muted width="880"></video>

1. **An issue** — a bug, labelled `ai-ready`
2. **It writes the code** — PR #1023, on a recorded base SHA
3. **The review says no** — one Major finding, naming `src/orbi/release.py:2377` and the fix
4. **It fixes itself** — more commits, until the checks go green
5. **It merges** — 9 commits, +427/−43, only after the review passed
6. **A tagged release** — v0.5.17, with the Issue and PR in the notes

Website <https://orbi.build> ｜ [Orbi Managed Cloud](https://orbi.build/cloud/?ref=gh-readme) ｜ Documentation <https://docs.orbi.build/> ｜ [Discussions](https://github.com/orbi-build/orbi/discussions) ｜ Docker [GHCR](https://ghcr.io/orbi-build/orbi) · [Docker Hub](https://hub.docker.com/r/orbibuild/orbi)

## Quick start

```bash
git clone https://github.com/orbi-build/orbi.git && cd orbi
uv tool install --force --reinstall --editable --python python3 .  # compatible system Python (>= 3.14, e.g. Fedora 43 / current Arch); older system Python (e.g. Ubuntu 24.04 ships 3.12): --python 3.14 so uv provisions it
```

Want just the CLI? Published on PyPI as [`orbi-cli`](https://pypi.org/project/orbi-cli/) (requires Python ≥ 3.14; the installed command stays `orbi`): `uv tool install orbi-cli` (on an older system Python, e.g. Ubuntu 24.04's 3.12, add `--python 3.14` so uv provisions a compatible interpreter) or `pip install orbi-cli` inside an activated Python ≥ 3.14 environment; verify with `orbi --version` → `orbi <version>`, uninstall with `uv tool uninstall orbi-cli`. To run Orbi itself, use the one-line installer at the top of [Getting started](docs/getting-started.mdx) — it creates the editable install Orbi's deployment drives.

### Ready check (before setup)

- `uv`: `uv --version`; Pi and its provider: `pi --version`, then `pi --print "reply with the single word: ok"`
- GitHub CLI ≥ 2.94 ([official repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md) — Ubuntu 24.04's package 2.45.0 is too old): run `gh auth login` once, then verify `gh auth status`
- Linux — systemd user session: `systemctl --user status`
- macOS — launchd GUI session: `launchctl print gui/$(id -u)` (not yet verified on real hardware; reports welcome)

Choose the mode in [Getting started](docs/getting-started.mdx): bootstrap uses this checkout as `repo_dir`; [External single-repo mode](docs/getting-started.mdx#external-single-repo-mode-deploy_home) uses it as `deploy_home` and a foreign repository as `repo_dir`.

```bash
cp src/orbi/example_config.toml orbi.toml
orbi setup --config orbi.toml  # 4. run one-time setup (checks prior gh auth, labels, scheduler units (systemd/launchd), and checkout; idempotent)
PYTHONPATH=src python3 -m orbi.runner --config orbi.toml  # 5. manually run one tick (for initial verification; the timer schedules normal runs)
orbi doctor --config orbi.toml  # 6. verify deployment health
```

## Why Orbi

- **GitHub Issues are the task pool**: the `ai-ready` label dispatches work, and the delivery record (comments, PRs, and CI) is complete by default, with no second task system;
- **Fully automated**: a user scheduler timer (systemd on Linux, launchd on macOS) triggers a tick every 5 minutes. Normal operation needs no status command, polling, or supervision;
- **Independent review + merge gates**: after a PR opens, an independent review session reviews it and fixes findings in the same session. Only the reviewed head can merge, and AI never merges or pushes protected branches;
- **Fail fast**: command errors fail immediately and leave the evidence in the logs. The Issue is marked `ai-blocked` for a human decision, with no silent fallback;
- **Observable end to end**: every journal log and GitHub progress comment carries the same `run_id`, so the complete timeline can be reconstructed with one grep.

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
- Supports `orbi add` for dispatching work, `status` for viewing the queue, `session` for following the Pi session, `install-units` for idempotently installing the scheduler units (systemd on Linux, launchd on macOS), and `doctor` for read-only diagnostics.

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
