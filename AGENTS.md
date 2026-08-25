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

## Review and fix loop (same PR)

- After a PR is opened, the Issue is in a recoverable review/fix state,
  not done: `ai-pr-opened` means awaiting review (a clean PR is never
  sent to the Fixer); a review finding, an advanced base, or a merge
  conflict moves it to the explicit `ai-fix-needed` state, which is a
  fixable state, never a reason to close the PR, re-claim the Issue, or
  open a replacement PR. A successful fix consumes `ai-fix-needed` and
  returns the Issue to `ai-pr-opened` (awaiting review again).
- The next tick resumes the same run on the same feature branch,
  worktree and PR number. Only `ai-fix-needed` Issues (not `ai-blocked`)
  are scanned for Fixer work; the fresh-claim scan excludes
  `ai-fix-needed` too, so a fix-pending Issue is never re-claimed as new
  work. The resume scene (run id, base, PR URL) is
  recovered from the latest `Muyan Pilot opened PR:` comment posted by
  a trusted maintainer (OWNER/MAINTAINER/MEMBER/COLLABORATOR; a public
  comment is never trusted). Branch and worktree are derived from the
  configured repo_dir, source repo, Issue number and run id — never
  read from a comment, so no comment can steer the runner into an
  arbitrary local path. A scene that cannot be recovered (missing
  field, no trusted comment) fails fast: the Issue is marked
  `ai-blocked` with the concrete reason, the tick stops, and no fresh
  task starts ahead of it — never guessed. Before any git/Pi mutation
  the configured base and the open PR (head repo, head branch, base,
  run marker, exact URL) are validated.
- When the latest remote base is not an ancestor of the delivery HEAD,
  the runner performs a plain `git merge origin/<base>` on the original
  branch and hands any conflict to the fixer; no auto conflict
  resolution, no `--abort`, no force push, no push of the protected
  branch.
- After a fix, the full test suite, 100% line/branch coverage, the real
  verification and the complete R1–R9 review run again before the same
  PR is re-verified.
- An unresolvable fix keeps the PR, branch and worktree intact and marks
  the Issue `ai-blocked` (removing `ai-fix-needed`) with the concrete
  conflict or finding.

## Run correlation

- One task attempt generates one run_id (8 hex chars) and reuses it for
  every later step of the attempt; a retry generates a new one. No new id
  system is introduced: no trace_id, no log_id, no second UUID, no
  tracing backend.
- Every journal line of the attempt starts with `[run_id]`, so one grep
  reconstructs the full timeline; every Issue/PR comment and the PR body
  carry the stable marker `<!-- muyan-pilot:run=<run_id> -->` plus the
  visible `run_id=` field; branch, worktree, Pi session dir and run
  artifacts carry it in their paths. A run-scoped event without a valid
  run_id fails fast.

## Git

- Work on the task feature branch.
- No merge. No push of `main` or `master`. Deliver through exactly one PR
  linked to the Issue.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues
  and labels are the only state store.
- No timeout on business tasks. systemd only schedules the tick and owns the
  run lifecycle.
