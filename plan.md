# Plan — Issue #80: v0.1 first release checklist (run cd855188)

## Goal

Close out the v0.1 release checklist: land every remaining 必须/顺手 item as
code (TDD, 100% line/branch coverage), perform the release-time GitHub
hygiene (close merged-but-open Issues, verify no stale `ai-in-progress`),
tag `v0.1.0`, and deliver exactly one PR whose body carries
`Fixes #80` so GitHub closes this Issue when the PR merges.

## What I inspected

- `bootstrap_runner.py` (2521 lines): `pick_issue` / `pick_in_progress_issue`
  / `pick_resumable_delivery` / `pick_next_delivery`, `stream_pi` (poll loop,
  model_wait, idle warning), `run_pi`, `process_issue` (progress wiring),
  `wait_for_delivery`, `main`.
- `progress.py` (`ProgressPublisher`: ensure/patch/milestone/finish),
  `pi_activity.py` (`SessionWatcher`: `stale_seconds`, `model_wait`),
  `muyan_pilot.py` (CLI: `add` / `status`), `systemd/muyan-pilot.{service,timer}`,
  `prompt.md`, `prompt_review.md`, `AGENTS.md`, `README.md`,
  `.muyan-pilot.example.toml`, `tests/` (16 files, 536 tests, 100% coverage).
- GitHub state of every checklist item (labels, comments, PRs):
  - Already merged into `main`: #82 (PR #87), #53 (PR #69), #81 (PR #88),
    #83 (PR #86), #70 (PR #85 — which ALSO implemented the `ai-pr-opened`
    resume scan required by #77), #54 (PR #68).
  - Still open and unimplemented (all `ai-ready`): #75, #77 (code already
    present via PR #85 — the Issue just was never closed), #78, #79, #52,
    #71, #73, #74, #49, #76.
  - `ai-merged` but still OPEN (need closing, #76): #18, #56, #58, plus #54
    (same state, PR #68 merged).
  - Stale `ai-in-progress` cleanup: the only Issue currently carrying
    `ai-in-progress` is #80 itself (this live run) — nothing stale to
    remove; recorded as evidence.
- Baseline: `536 passed`, 100% line/branch coverage on
  `/usr/bin/python3 -m coverage run --branch -m pytest tests/ -q`.

## Repository decision

`xqliu/muyan-pilot` (the configured source repo; the checklist is about the
Pilot itself). No new repo, no new framework.

## Tasks

### Code (TDD: failing test first, then minimal implementation)

- [ ] #71 — `pick_issue`: scan `label:ai-ready label:bug` first (same
      exclusions), then the existing ready scan. No priority numbers, no
      new state.
- [ ] #75 — `stream_pi`: while `model_wait` and the session JSONL is frozen
      for `PI_MODEL_WAIT_DEAD_SECONDS` (default 600 s), the upstream
      (llama/proxy) is dead: kill Pi, log `run_failed
      reason=upstream_dead_stale_…`, raise (fail fast → normal failure path
      releases the slot). Never fires while events keep arriving (no
      business timeout).
- [ ] #77 — code already in `main` via PR #85 (`pick_resumable_delivery`
      scans `ai-pr-opened` too); add a regression test pinning the
      `ai-pr-opened` scan + `main` routing into `wait_for_delivery`, and
      close the Issue from the delivery evidence (no code change needed).
- [ ] #79 — `process_issue`: `publisher.ensure` / `started` milestone /
      plan+test milestones / `run_end` publishing become best-effort
      (try / log `progress_publish_failed`, never skip `run_pi` /
      `wait_for_delivery`, never `ai-blocked`). The `Muyan Pilot opened PR:`
      scene comment keeps its current (post-#60) behavior — it is the
      resume contract, not a bypass.
- [ ] #74 — `muyan_pilot.py session [--follow] [--pretty]`: print the
      newest `.pi-session/*.jsonl` under `repo_dir/.worktrees`; `--follow`
      tails the file selected at start (no mid-run switching); no session
      file → fail fast, non-zero exit. README gets the two lines.
- [ ] #52 — `systemd/muyan-pilot.service`: `ExecStartPre` fetch +
      `--ff-only` merge of `origin/main` before `ExecStart`; README/AGENTS
      note that code updates take effect at the next Runner start.
- [ ] #78 — wording only: `prompt.md` (drop the pre-PR "complete
      review-fix loop" sentence), `AGENTS.md` (same), `run_pi` docstring /
      any remaining "complete review" text in the implementer path. No
      `tdd-dev` skill changes.
- [ ] #73 — `prompt.md` + `prompt_review.md`: hard rule "verify external
      behavior against official docs / `--help` before writing it; test
      assertions must assert the real contract, not the implementation's
      guessed shape; bypass failures (progress, notifications) only log
      and never decide delivery outcome". Reviewer checks this explicitly.
- [ ] #49 — README/AGENTS documentation: label table (name/meaning/enter/
      leave + repo initialization), run marker + recovery scene contract
      (trusted maintainer, PR body marker, legacy PRs), the automatic loop
      state machine, today's behaviors (PR #47 fixer, `ai-fix-needed`,
      fail-fast malformed scene, session identification, review verdict on
      GitHub, label creation, post-merge main sync). A test verifies the
      docs reference only existing labels / current flow names.

### Release hygiene (no code)

- [ ] #76 — `gh issue close` #18, #56, #58 (and #54, same state) with a
      comment carrying the merged-PR URL.
- [ ] Stale `ai-in-progress` check: verify only #80 (this run) carries the
      label; record evidence.
- [ ] Close #77 with evidence (PR #85 already merged the fix).

### Delivery

- [ ] Full suite + 100% coverage on `/usr/bin/python3`.
- [ ] Tag `v0.1.0` (annotated, on the task branch).
- [ ] Push the task branch; open exactly one PR with
      `<!-- muyan-pilot:run=cd855188 -->` and `Fixes #80` in the body.

## Verification

```bash
/usr/bin/python3 -m coverage run --branch -m pytest tests/ -q
/usr/bin/python3 -m coverage report --fail-under=100 --show-missing
/usr/bin/python3 muyan_pilot.py session --config <tmp toml>   # fail fast, no session
git fetch origin main && git merge-base --is-ancestor origin/main HEAD
```

Plus per-item evidence recorded in `test.log` and the PR body.
