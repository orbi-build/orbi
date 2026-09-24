# Orbi Docker deployment (community-maintained, unofficial support)

English | [简体中文](README.zh-CN.md)

> **Community-maintained / unofficial.** This directory (`3rd/docker/`) is a
> thin Docker wrapper around Orbi, maintained by the community and **not
> part of the officially supported surface**. The core deployment path is
> the host systemd user unit/timer (see
> [docs/getting-started](https://docs.orbi.build/getting-started));
> pick ONE of the two — never manage the same task pool with both on the
> same machine. Troubleshoot on your own first; official Issues
> prioritize the core path.

One `docker run` on Linux starts an Orbi runner: inside the container
runs the **unmodified official deployment flow** — systemd as PID 1 with
a real user manager for the `orbi` user, the official
`systemd/orbi@.service` + `orbi@.timer` templates installed as-is,
`orbi setup` executed as-is, and `orbi@1.timer` firing one runner tick
every 5 minutes. Authentication is injected via environment variables,
all state lives in two volumes, the container itself is stateless, and
stop/rebuild loses nothing.

## Prerequisites

- Docker on a Linux host (cgroup v2);
- a GitHub token with write access to the task pool repo (classic token
  with `repo` scope, or fine-grained token with Issues/PR/Contents
  read+write);
- the task pool repo name (`owner/repo`);
- for a real delivery, one model endpoint API key (the Quick start uses
  DeepSeek; any OpenAI-compatible endpoint works).

## Quick start

Pull the published image. Both registries carry the same tags: `latest`
and the release number without the `v` prefix (e.g. `0.5.17`).

```bash
docker pull ghcr.io/orbi-build/orbi:latest
# the same image on Docker Hub:
docker pull docker.io/orbibuild/orbi:latest
```

Start the runner with ONE command — token, task pool and model provider
all come from environment variables; the entrypoint turns them into the
deploy-home env file, the generated `orbi.toml` and the generated
`pi-providers.json` on first start (existing files are never
overwritten). Substitute the three quoted values — token, repo, key; the
DeepSeek endpoint is an example, any OpenAI-compatible endpoint works
through the same four variables (`ORBI_PI_PROVIDER`, `ORBI_PI_MODEL`,
`ORBI_PI_BASE_URL`, `ORBI_PI_API_KEY`):

```bash
# Optional model limits with their defaults — add them as -e lines to the
# command below (before the image reference) only to override:
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
  -e ORBI_PI_MODEL=deepseek-flash \
  -e ORBI_PI_BASE_URL=https://api.deepseek.com \
  -e ORBI_PI_API_KEY="sk-xxx" \
  ghcr.io/orbi-build/orbi:latest
```

`--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw` lets the systemd
inside the container (PID 1) create its own scope on the host cgroup
tree — without it systemd cannot start (verified: `/init.scope` creation
fails under the default private cgroup namespace; no `--privileged`
needed).

One command, no docker compose (Orbi is a single runner service). The
Docker-level flags:

| Flag | Required | Purpose |
|---|---|---|
| `-v orbi-deploy:/orbi` | yes | the deploy-home volume (what lives in it: the configuration table below) |
| `-v orbi-work:/work` | yes | the delivery-checkout volume (what lives in it: the configuration table below) |
| `-v /sys/fs/cgroup:/sys/fs/cgroup:rw` | yes | systemd (PID 1) needs a writable cgroup subtree; without it the container cannot start |
| `--stop-signal SIGRTMIN+3` | recommended | systemd's shutdown signal; `docker stop -t 30` gives it time to finish |
| `--tmpfs /run --tmpfs /tmp` | recommended | the writable runtime dirs systemd expects |

## How configuration reaches the runner

| You provide | How it reaches the runner |
|---|---|
| `ORBI_SOURCE_REPO` | The task pool repo. On first start the entrypoint clones it into the `/work` volume and writes it into the generated `orbi.toml`. Alternative: bind-mount an existing checkout (`-v ~/projects/myrepo:/work`) — the host uid must match the container's `orbi` user (uid 1000), or git fails with dubious ownership; named volumes avoid the issue. |
| `GH_TOKEN` | The entrypoint writes it to `/orbi/.orbi/env` (the systemd `EnvironmentFile`, mode 600), regenerated from the injected variables on every start. At tick time gh reads it there and the gh credential helper supplies git over HTTPS — no SSH key anywhere; the token is never baked into an image layer. It needs **write access to the task-pool repo**: a classic token with the `repo` scope, or a fine-grained token with Issues, pull requests and contents read+write (the runner creates labels, branches and PRs with it). |
| `ORBI_PI_PROVIDER`, `ORBI_PI_MODEL`, `ORBI_PI_BASE_URL`, `ORBI_PI_API_KEY` | The four model-provider values (set all four or none — a partial set fails the start, naming the missing ones). On every start the entrypoint regenerates `/orbi/.orbi/env` from the injected variables, writing `PI_API_KEY` into it — a changed `ORBI_PI_API_KEY` takes effect on the next `docker run`/`docker restart` with no manual edit; `/orbi/.orbi/pi-providers.json` (one provider and one model whose `apiKey` references `$PI_API_KEY` — the key value never lands in the JSON) and the `pi_provider`, `pi_model`, `pi_providers` lines in `orbi.toml` are generated on first start only and never overwritten. |
| `ORBI_PI_API`, `ORBI_PI_CONTEXT_WINDOW`, `ORBI_PI_MAX_TOKENS` | Optional limits for the generated `pi-providers.json`; defaults `openai-completions`, `128000`, `16384`. |
| `ORBI_BASE_BRANCH` | Optional delivery base branch (default `main`). |
| `ORBI_ENV_<NAME>` | Optional, repeatable: forwarded to `/orbi/.orbi/env` as `<NAME>=<value>` (e.g. `ORBI_ENV_GROQ_API_KEY` lands as `GROQ_API_KEY`) so a provider file can reference it — see Advanced: several providers. |
| `orbi-deploy` volume → `/orbi` | The deploy-home volume: the orbi source checkout (`engine_source_track = "release"` follows the latest release automatically), the generated `orbi.toml`, the `.orbi/env` credential file and the `.orbi/` runtime state. |
| `orbi-work` volume → `/work` | The delivery-checkout volume: the task-pool clone, the task worktrees under `/work/.worktrees/` and the slot locks under `/work/.orbi/`. |

First-start flow: the entrypoint validates the injected variables →
clones the orbi source into `/orbi` (reused when present) → generates
`/orbi/orbi.toml` (**never overwrites an existing one** — mount your own
freely) → writes `/orbi/.orbi/env` → clones the task pool into `/work`
(reused when present) → `gh auth setup-git` (HTTPS transport, git
credentials through the gh credential helper, no SSH key anywhere) →
the official editable CLI install `uv tool install --editable /orbi` →
hands over to systemd → the oneshot unit starts the user manager and
runs the official `orbi setup` (idempotent, re-runs on every start) →
the timer takes over, one tick every 5 minutes.

The generated `orbi.toml` (with the Quick start variables):

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
pi_model = "deepseek-flash"
```

## First delivery

1. In the task pool repo, open an Issue stating ONE runtime outcome in
   one of the two shapes: a fix (`when X, should Y, actually Z`) or a
   change (`when X, the user should be able to Y; today they cannot
   because Z`).
2. Add the `ai-ready` label (the first start created the twelve
   platform labels).
3. Follow the tick log:

```bash
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  journalctl --user -u orbi@1.service -f
```

Within two ticks (the timer fires every 5 minutes) the Issue walks
`ai-ready` → `ai-in-progress` → `ai-pr-opened` and a PR opens; it merges
(`ai-merged`) after the independent review round. No `docker exec`
configuration step is needed anywhere in this path — one that is
required is a docs or entrypoint defect.

## Orbi quick guide

The delivery vocabulary in one place; the full reference lives in the
[workflow docs](https://docs.orbi.build/workflow).

- **The task** — an Issue stating ONE runtime outcome in one of the
  two shapes: a fix (`when X, should Y, actually Z`) or a change
  (`when X, the user should be able to Y; today they cannot because
  Z`), plus a short acceptance list: the user journey as precondition
  → command/configuration → the success the user sees → the failure
  path with its repair.
- **The labels** — `ai-ready` is the execution switch: the runner only
  picks up labelled Issues (pickup order: `p0` first, then `bug`, then
  plain). A claimed Issue walks `ai-in-progress` → `ai-pr-opened` →
  `ai-merged` (terminal; the PR body's `Fixes #N` closes the Issue) or
  `ai-fix-needed` (the next review round fixes the finding); `ai-blocked`
  (terminal) needs a human decision. `ai-epic` marks a coordination-only
  Issue the runner never claims, and `ai-release` marks a release task
  (below). The runner manages the delivery states; `ai-ready`, `ai-epic`,
  `ai-release`, `p0` and `bug` are yours to add and remove.
- **Watching** — the run keeps exactly one progress comment on the
  Issue, updated in place at every milestone, and the tick log streams
  through the `journalctl` command above.
- **What to expect** — within two ticks the branch and PR exist
  (`Fixes #N` in the body), an independent review session runs (at most
  5 rounds), and the merge lands exactly the reviewed head — GitHub
  then closes the Issue.
- **Releases** — a release is its own Issue labelled `ai-ready` +
  `ai-release` whose body declares a `## Release` block: `version`,
  `base_branch`, and a `scope` (a list of `#N` items) or
  `scope_from_milestone`. The Runner's release state machine freezes
  the base, waits for green CI, bumps the version, tags, publishes the
  GitHub Release and closes the matching Milestone — no PR; any failure
  stops at `ai-blocked` for a human decision.

## State persistence

All state lives in the two volumes described in the configuration table
above and survives container rebuilds.

How to verify: `docker stop` → `docker rm` → run the same `docker run`
again with the **same volumes and variables**: `orbi setup` re-runs
idempotently (`cli=verified`), the `.orbi/` state and worktrees are
intact, and the timer keeps working. A run interrupted by a container
restart has the same semantics as a host power cycle: the next tick's
restart-resume scan picks it up.

## Build from source

When you need to change the wrapper itself (the entrypoint, the setup
oneshot, the Dockerfile), build the image from a repository checkout:

```bash
git clone https://github.com/orbi-build/orbi.git
cd orbi
docker build -t orbi-docker 3rd/docker
```

Image contents: the `python:3.14-slim` base + Python 3.14 + git + the gh
CLI + uv + systemd (user session) + Node.js + Pi (the development agent
from the official prerequisites table, installed per the pi.dev docs
with `npm install -g --ignore-scripts`). The image contains **no
credentials**.

Then run the same `docker run` as in the Quick start with `orbi-docker`
as the image name.

## Advanced: several providers

The Quick start configures ONE provider from environment variables. To
serve several (e.g. deepseek plus groq):

1. Pass the extra keys at `docker run` time via `ORBI_ENV_<NAME>`
   (repeatable): `-e ORBI_ENV_GROQ_API_KEY="gsk-xxx"` lands in
   `/orbi/.orbi/env` as `GROQ_API_KEY`.
2. Edit `/orbi/.orbi/pi-providers.json` — the entrypoint generated it
   with one provider and never overwrites an existing file — and add the
   other providers, each `apiKey` referencing its env variable
   (`"$GROQ_API_KEY"`).
3. Point `pi_provider` / `pi_model` in `/orbi/orbi.toml` at the provider
   and model to use, then restart the container: `docker restart orbi`
   (both files are yours — the entrypoint never overwrites an existing
   config).

If the first start had no provider variables at all, no
`pi-providers.json` was generated; `orbi setup` scaffolds a starter file
to fill in instead. Verify an endpoint before relying on it (provider
and model as in `pi-providers.json`; a real API reply means endpoint and
key work, 401 or a timeout is a key or network problem):

```bash
docker exec -u orbi orbi bash -c '. /orbi/.orbi/env && pi --provider deepseek \
  --model deepseek-flash --api-key "$PI_API_KEY" \
  --print "reply with the single word: ok"'
```

## Operations

In-container commands always run with `-u orbi` (the user the runner
runs as); `systemctl --user` / `journalctl --user` depend on the user
session bus, which `docker exec` does not carry automatically — hence
`-e XDG_RUNTIME_DIR=/run/user/1000`. The orbi CLI installs to
`/home/orbi/.local/bin/` (not on root's PATH).

```bash
docker logs -f orbi                                   # setup result (setup=ok/setup_failed) + systemd console
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  journalctl --user -u orbi@1.service -n 50 --no-pager  # tick logs
docker exec -u orbi -w /orbi orbi /home/orbi/.local/bin/orbi status  # queue and current task
docker exec -u orbi -w /orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  /home/orbi/.local/bin/orbi doctor                   # deployment health check
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  systemctl --user list-timers                        # the next tick time
docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi \
  systemctl --user start orbi@1.service               # trigger a tick immediately
docker stop -t 30 orbi && docker rm orbi              # stop and remove (volumes kept)
docker volume rm orbi-deploy orbi-work                # wipe all state
```

Upgrading: `docker pull` the newer tag and recreate the container with
the same volumes and variables — state survives in the volumes and the
entrypoint plus `orbi setup` re-converge. (If you run a self-built
image, `docker build` the new checkout and recreate the same way.)

## Failure and repair

| Symptom | Cause and repair |
|---|---|
| The container exits immediately, `docker logs` shows `GH_TOKEN is required` / `ORBI_SOURCE_REPO is required` | Missing injected variables. Add the `-e` flags from the Quick start and recreate |
| `partial Pi provider configuration; missing ...` | Some but not all of the four model-provider variables are set. Set all four or none (see the configuration table above) and recreate |
| `invalid ORBI_SOURCE_REPO ...` | The value is not in `owner/repo` form |
| `cloning ... failed` | The token cannot read the repo, or the network is down. Fix and recreate |
| setup shows `repo=... permission=READ` or a permission error | The token lacks write access to the task pool (needs to create Issue labels / branches / PRs). Switch tokens and recreate |
| `orbi setup` fails (`setup_failed reason=...`) | Fix per the reason in the output (usually token permissions or transport), then `docker restart orbi` to re-run the idempotent setup |
| tick log `transport_unreachable` | The HTTPS credentials went stale: confirm `GH_TOKEN` is still valid, then recreate the container (the entrypoint rewrites the env file) |
| tick log `unit_drift` followed by `auto_synced` | Normal self-heal: after an official template update the next tick reinstalls the units; nothing to do |
| `docker exec -u orbi -e XDG_RUNTIME_DIR=/run/user/1000 orbi systemctl --user is-active` fails | The in-container user manager is unhealthy; `docker restart orbi`. If it recurs, file an Issue in the repository (note: docker wrapper, unofficial) |

## Design notes (why the scheduler is not re-implemented)

Orbi's execution model depends on `systemctl --user` timer scheduling:
`orbi setup` probes the user bus before starting, and the pre-tick
unit-drift self-heal runs `systemctl --user daemon-reload`. So the
container does not re-create a scheduler with cron/supervisor — it runs
systemd (PID 1) plus the real `user@1000.service` user manager, and the
official templates, `orbi setup`, the timer and the self-heals all work
as-is. This directory stays a thin wrapper: prepare the volumes, inject
the credentials, install the CLI, hand over to the official flow.

## Relationship with the core systemd deployment

**Pick one.** The Docker wrapper targets "fast deployment on platforms
without a user systemd" (NAS, container cloud platforms, etc.); on a
Linux host with a systemd user session, use the
[official deployment path](https://docs.orbi.build/getting-started).
Never point both paths at the same task pool repo (two runners would
compete for the same tickets).
