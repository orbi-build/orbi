# AGENTS.md

Development contract for this repository. Every local Pi bootstrap run must
follow it before changing code.

## Read first

- Read the GitHub Issue, the configured context files, `README.md`, and the
  relevant code before touching anything.

## TDD and coverage

- TDD: write a failing test first, then the smallest implementation, then
  refactor.
- Python code keeps 100% line and branch coverage:

  ```bash
  /usr/bin/python3 -m coverage run --branch -m pytest tests/ -q
  /usr/bin/python3 -m coverage report --show-missing
  ```

## UI work

- Any UI task must drive the real running app with Playwright: real
  interaction, assertions on the changed flow, console and network error
  checks, and screenshots saved under the run artifacts.

## Fail fast

- Command errors fail fast: log the command, return code, stdout and stderr,
  then raise. Never swallow an error or add a fallback path.

## Base freshness

- Every task worktree is created from the frozen `origin/<base_branch>` SHA
  (default `main`), never from the main worktree's current HEAD.
- Branch and worktree names carry the unique run id, so a retried Issue gets
  a new independent run and the old scene is preserved.
- Before creating the PR, re-fetch `origin/<base_branch>`; if the base
  advanced, merge it into the task branch, resolve conflicts manually, rerun
  the full tests and the complete review-fix loop, then push the task branch.
- The runner rejects a delivery whose HEAD does not contain the latest remote
  base. No auto conflict resolution, no force push, no merge or push of the
  protected branch.

## Git

- Work on the task feature branch.
- Pi (the implementer) does not merge and does not push `main` or `master`.
  It delivers through exactly one PR linked to the Issue; the Runner is the
  only merge actor (see below).

## Auto review, fix and merge

- After the implementer opens the PR, the Runner freezes the exact PR
  base/head SHA and runs an independent, read-only review session
  (code-review R1–R9) against those SHAs. The reviewer ends with one
  machine-readable `REVIEW_VERDICT` line; a missing or malformed verdict fails
  fast and is never treated as a pass.
- While Blocker/Major findings exist, the Runner comments them to the Issue and
  PR, runs a fixer on the same feature branch/worktree, re-freezes the SHA,
  reruns the full suite, and re-reviews. The loop is bounded (5 rounds); if it
  exhausts rounds with findings it fails fast and marks the Issue `ai-blocked`.
- The merge gate re-fetches the latest remote base and requires the PR head to
  contain it, the PR to be mergeable, and the remote head to still be the
  reviewed head; it then merges with `gh pr merge --match-head-commit` so only
  the reviewed head lands. A PR behind the latest base is rejected, never
  merged. The Runner confirms the PR is MERGED and the merge commit is on the
  protected branch before marking the Issue `ai-merged`.
- A review finding is not `ai-blocked`: it enters the same PR's fix/review
  loop. Only command failure, an unavailable environment, or a fix that cannot
  be verified fails fast and marks `ai-blocked`.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues
  and labels are the only state store.
- No timeout on business tasks. systemd only schedules the tick and owns the
  run lifecycle.
