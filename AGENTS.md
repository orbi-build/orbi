# AGENTS.md

Development contract for this repository. Every local Pi bootstrap run must
follow it before changing code.

Four audiences, one file:

- **Issue authors** (humans or the monitor): granularity below.
- **Implementer Pi**: read first, TDD, one PR.
- **Reviewer Pi**: a new `pi --print` after the PR, `prompt_review.md`,
  a new JSONL.
- **Runner**: labels, base freshness, observability, the post-PR
  review/fix/merge loop.

## Read first

- Read the GitHub Issue, the configured context files, `README.md`, and the
  relevant code before touching anything.

## Issue granularity

Write GitHub Issues so one Pilot implement session can finish them. One
Issue is **one runtime outcome** (when X, should Y, actually Z).

- Size: one observable behavior, a handful of related files, tests
  included, hundreds of lines. Title should work as a test name.
- Open the Issue once the root cause is pinned.

## Implement vs review

- Implementer session: plan, TDD, tests, push one PR.
- After the PR exists, the Runner starts independent review: a new
  `pi --print` with `prompt_review.md` and a new JSONL, on the same
  worktree.

## TDD and coverage

- TDD: write a failing test first, then the smallest implementation, then
  refactor.
- External APIs, CLI flags, and HTTP paths are asserted against official docs
  or one real call.
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
  then raise.

## Git

- Work on the task feature branch. Pi pushes that branch and opens exactly
  one PR linked to the Issue. The Runner is the only merge actor on `main`
  and `master` (see **After the PR**).
- The PR description must contain `Fixes #<issue-number>` (it may be on
  the first line), pointing at the source Issue so GitHub closes the Issue
  when the PR merges into the default branch. GitHub reads the keyword from
  the PR body and from commit messages; the PR title is not read. The
  runner accepts a PR whose body contains it.

## Base freshness

- Every task worktree is created from the frozen `origin/<base_branch>` SHA
  (default `main`).
- Branch and worktree names carry the unique run id, so a retried Issue gets
  a new independent run and the old scene is preserved.
- Before creating the PR, re-fetch `origin/<base_branch>`; if the base
  advanced, merge it into the task branch, resolve conflicts manually, rerun
  the full test suite, then push the task branch.
- The runner accepts a delivery whose HEAD contains the latest remote base.
  Conflicts are resolved with a plain `git merge` on the task branch.

## Run correlation

- One task attempt generates one run_id (8 hex chars) and reuses it for
  every later step of the attempt; a retry generates a new one.
- Every journal line of the attempt starts with `[run_id]`, so one grep
  reconstructs the full timeline; every Issue/PR comment and the PR body
  carry the stable marker `<!-- muyan-pilot:run=<run_id> -->` plus the
  visible `run_id=` field; branch, worktree, Pi session dir and run
  artifacts carry it in their paths. Every run-scoped event includes a
  valid run_id; a missing one fails fast.

## Automatic observability

- Normal operation publishes progress automatically.
  `muyan_pilot.py status` is a debug attachment.
- Journal: while a session runs, the journal gets a heartbeat at most every
  30 seconds and an immediate event on phase/action change. Every line
  carries issue, run id, role (implement/review/fix/merge), phase, elapsed,
  last activity, last action, session and branch. Five minutes without
  model/session activity logs an idle warning; the first new activity after
  it logs a resumed event.
- GitHub: exactly one progress comment per run, carrying a hidden run
  marker. It is PATCHed in place (at most every 30 seconds or on progress
  change). Milestones (started, plan ready, tests passed/failed, review
  findings, fix pushed, PR opened, merged, blocked) are short standalone
  comments so GitHub Mobile pushes a notification. After a process restart
  the same comment is found by the run marker and kept. On success the
  comment becomes the final delivery summary (PR, tests, review evidence);
  on failure it becomes the blocked scene with the next-step reason.
- Once the PR exists (verified and labeled `ai-pr-opened`), the delivery is
  complete: progress-publishing failures (the delivered PATCH, the `PR
  opened` milestone, the opened-PR scene comment) are logged as
  `progress_publish_failed` and the run continues into the independent
  review/merge loop (Issue #60).

## Task dependencies (blockedBy)

- Task dependencies use GitHub's native `blockedBy` relation
  (`gh issue edit N --add-blocked-by M`). The runner reads that field, not
  the Issue body.
- Before claiming an `ai-ready` Issue the runner reads `blockedBy`
  (`gh issue list --json blockedBy`). Open blockers (blocker nodes with
  `state: "OPEN"`) leave the Issue unclaimed: a structured
  `blocked_by issue=N repo=... blockers=M1,M2` log line is written and
  the next ready Issue of the same repo is considered. A closed blocker
  is inert: GitHub keeps the relation listed with `state: "CLOSED"`
  (verified against the live API) and the runner counts only open
  blockers — the next tick claims the Issue.
- A failed `blockedBy` query logs `blocked_by_check_failed`, claims
  nothing from that repo, and retries on the next tick.
- Single-slot serial execution reads the field, skips, and waits.

## After the PR: review, fix, and merge

The implementer is done when the PR exists. Everything below is the
Runner.

- After a PR is opened, the Issue is in a recoverable review/fix state:
  `ai-pr-opened` means awaiting review. The Runner freezes the exact PR
  base/head SHA and starts an independent, read-only review session
  (code-review R1–R9) against those SHAs — a new `pi --print`,
  `prompt_review.md`, a new JSONL. The reviewer ends with one
  machine-readable `REVIEW_VERDICT` line; a missing or malformed verdict
  fails fast.
- A review finding, an advanced base, or a merge conflict moves the
  Issue to `ai-fix-needed`. A successful fix consumes `ai-fix-needed` and
  returns the Issue to `ai-pr-opened` (awaiting review again). Command
  failure, an unavailable environment, or a fix that cannot be verified
  marks `ai-blocked`.
- The next tick resumes the same run on the same feature branch,
  worktree and PR number. The Fixer scan is `ai-fix-needed` Issues; the
  fresh-claim scan skips those. The resume scene (run id, base, PR URL)
  is recovered from the latest `Muyan Pilot opened PR:` comment posted by
  a trusted maintainer (OWNER/MAINTAINER/MEMBER/COLLABORATOR). Branch and
  worktree are derived from the configured repo_dir, source repo, Issue
  number and run id. A scene that cannot be recovered (missing field, no
  trusted comment) fails fast: the Issue is marked `ai-blocked` with the
  concrete reason and the tick stops. Before any git/Pi mutation the
  configured base and the open PR (head repo, head branch, base, run
  marker, exact URL) are validated.
- When the latest remote base is not an ancestor of the delivery HEAD,
  the runner performs a plain `git merge origin/<base>` on the original
  branch and hands any conflict to the fixer.
- While Blocker/Major findings exist, the Runner comments them to the
  Issue and PR, runs a fixer on the same feature branch/worktree,
  re-freezes the SHA, reruns the full suite, and re-reviews. After a
  fix, the full test suite, 100% line/branch coverage, the real
  verification and the complete R1–R9 review run again before the same
  PR is re-verified. The loop is bounded (5 rounds); if it exhausts
  rounds with findings it marks the Issue `ai-blocked`. An unresolvable
  fix keeps the PR, branch and worktree intact and marks the Issue
  `ai-blocked` (removing `ai-fix-needed`) with the concrete conflict or
  finding.
- The merge gate re-fetches the latest remote base and requires the PR
  head to contain it, the PR to be mergeable, and the remote head to
  still be the reviewed head; it then merges with
  `gh pr merge --match-head-commit` so only the reviewed head lands. The
  Runner confirms the PR is MERGED and the merge commit is on the
  protected branch before marking the Issue `ai-merged`.

## Scope

- State is GitHub Issues and labels.
- systemd schedules the tick and owns the run lifecycle.
