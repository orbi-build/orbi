# AGENTS.md

Development contract for this repository. Every local Pi bootstrap run must
follow it before changing code.

Four audiences, one file:

- **Issue authors** (humans or the monitor): granularity below.
- **Implementer Pi**: read first, TDD, tests, and one PR.
- **Reviewer Pi**: a new `pi --print` after the PR, `prompt_review.md`, and a
  new JSONL.
- **Runner**: labels, observability, review/fix, and merge.

## Issue granularity

Write GitHub Issues so one Pilot implement session can finish them. One Issue
is **one runtime outcome** (when X, should Y, actually Z).

- Size: one observable behavior, a handful of related files, tests included,
  hundreds of lines. Title should work as a test name.
- Open the Issue once the root cause is pinned.

## Implement vs review

- Implementer session: plan, TDD, tests, push one PR.
- After the PR exists, the Runner starts independent review: a new
  `pi --print` with `prompt_review.md` and a new JSONL on the same worktree.

## Read first

- Read the GitHub Issue, the configured context files, `README.md`, and the
  relevant code before touching anything.

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
  then raise. Never swallow an error or add a fallback path.

## Automatic observability

- Normal operation publishes progress automatically: no human status
  command, no polling, no supervision. `muyan_pilot.py status` is a debug
  attachment only — never part of the normal workflow or acceptance
  evidence.
- Journal: while a session runs, the journal gets a heartbeat at most every
  30 seconds and an immediate event on phase/action change. Every line
  carries issue, run id, role (implement/review/merge), phase, elapsed,
  last activity, last action, session and branch. No model/session activity
  for 5 minutes logs an idle warning; the first new activity after it logs
  a resumed event.
- GitHub: exactly one progress comment per run, carrying a hidden run
  marker. It is PATCHed in place (at most every 30 seconds or on progress
  change) and never replaced by new heartbeat comments. Milestones (started,
  plan ready, tests passed/failed, review findings, PR opened,
  merged, blocked) are short standalone comments so GitHub Mobile pushes a
  notification. After a process restart the same comment is found by the
  run marker and kept — no database. On success the comment becomes the
  final delivery summary (PR, tests, review evidence); on failure it becomes
  the blocked scene with the next-step reason.
- The GitHub progress comment is a pure bypass (Issue #79): every
  `ProgressPublisher` step (ensure / live patch / milestone / finish, in
  the implement and review roles alike) fails as
  `progress_publish_failed` and never fails the delivery, never marks the
  Issue `ai-blocked`, and never skips `run_pi` / `wait_for_delivery` —
  the journal is the record, the comment is observability (Issue #60
  first applied this to the post-PR record). The `Muyan Pilot opened PR:`
  scene comment is NOT a bypass: the next tick's resume parses it
  (Issue #45/#89), so a failure there fails the delivery fail-fast.

## Base freshness

- Every task worktree is created from the frozen `origin/<base_branch>` SHA
  (default `main`), never from the main worktree's current HEAD.
- Branch and worktree names carry the unique run id, so a retried Issue gets
  a new independent run and the old scene is preserved.
- Before creating the PR, re-fetch `origin/<base_branch>`; if the base
  advanced, merge it into the task branch, resolve conflicts manually, rerun
  the full tests, then push the task branch (the independent review runs
  after the PR is opened and absorbs any further base advance in-session).
- The runner rejects a delivery whose HEAD does not contain the latest remote
  base. No auto conflict resolution, no force push, no merge or push of the
  protected branch.
- A delivery is acceptable only when its HEAD contains the latest remote base;
  base updates use a plain `git merge` on the task branch.
- The Runner's own code updates at the next service start (Issue #52):
  `muyan-pilot.service` runs `ExecStartPre` =
  `git fetch origin main && git merge --ff-only origin/main` in the main
  checkout before `ExecStart`. A dirty checkout, a failed fetch or a
  non-fast-forwardable state fails the preflight: the service does not
  start and the reason lands in the systemd journal (fail fast). A
  currently running long task is never hot-updated or killed — while the
  service is active, systemd ignores the timer's start request, and the
  next real start runs the latest code. No refresh service, worker,
  dispatcher or resident process is added; the 15-minute timer is
  unchanged.

## Task dependencies (blockedBy)

- Task dependencies use GitHub's native `blockedBy` relation
  (`gh issue edit N --add-blocked-by M`); never write `Depends on #N`
  in the Issue body — the body is not part of `blockedBy` and the
  runner does not parse body dependencies.
- Before claiming an `ai-ready` Issue the runner reads `blockedBy`
  (`gh issue list --json blockedBy`). Open blockers (blocker nodes with
  `state: "OPEN"`) mean the Issue is not claimed: no `ai-in-progress`,
  no label change, no worktree; a structured
  `blocked_by issue=N repo=... blockers=M1,M2` log line is written and
  the next ready Issue of the same repo is considered. A closed blocker
  no longer blocks: GitHub keeps the relation listed with
  `state: "CLOSED"` (inert, verified against the live API) and the
  runner counts only open blockers — the next tick claims the Issue
  with no bookkeeping.
- A failed `blockedBy` query fails open (treated as unblocked: the tick
  claims nothing from that repo, logs `blocked_by_check_failed`, and
  the next tick retries) — an API error must never deadlock the queue.
- No DAG, topological sort, or multi-worker scheduling: single-slot
  serial execution only reads the field, skips, and waits.

## Review, in-session fix and merge (same PR)

- The review session is independent (a new Pi process, `prompt_review.md`,
  a new JSONL) and, since Issue #82, ALSO the fixer: the reviewer may
  modify code, run the full test suite with 100% line/branch coverage,
  commit, and push ONLY the task branch — then re-emit the
  `REVIEW_VERDICT` for the fixed head. There is no cold-start Fixer and
  no third review session: a `pass` verdict means zero Blocker/Major
  findings AFTER the in-session fixes. The review prompt never attaches
  the `review-fix-loop` or `tdd-dev` skills: the reviewer applies the
  code-review R1–R9 criteria directly and fixes findings in-session
  (no nested review/fix loop).
- After a PR is opened the Issue is in a recoverable review state, not
  done: `ai-pr-opened` means awaiting review. `ai-fix-needed` marks a
  delivery whose head is not mergeable yet (the review found a finding
  the session could not fix, or the PR is behind the latest base / has a
  merge conflict): the NEXT tick resumes the same run on the same
  feature branch, worktree and PR number and runs the next independent
  review session, which merges the latest `origin/<base>` into the
  branch IN-SESSION, resolves any conflict, re-runs the full suite and
  re-emits the verdict. Both opened-PR states are scanned (Issue #70).
  The `ai-pr-opened` scan exists because the delivery that opened the
  PR can be gone (a killed runner, or the progress failure behind Issue
  #70 that used to block the Issue before the review started): without
  it a valid MERGEABLE PR is stranded with no owner. `ai-fix-needed` is
  never a reason to close the PR, re-claim the Issue, or open a
  replacement PR; a successful merge moves the Issue to `ai-merged`.
- `ai-blocked` Issues are excluded (they need a human decision first),
  as are merged and in-flight Issues; the fresh-claim scan excludes
  both opened-PR states, so an opened-PR Issue is never re-claimed as
  new work. The resume scene (run id, base, PR URL) is
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
- The Harness merge gate is unchanged: it re-fetches the latest remote
  base and requires the PR head to contain it, the PR to be mergeable,
  and the remote head to still be the reviewed head. After a clean
  verdict the PR is RE-FROZEN (the reviewer may have pushed an
  in-session fix, advancing the head) and the gate runs against the
  re-frozen head; `gh pr merge --match-head-commit` then lands exactly
  that head. No auto conflict resolution by the Runner, no `--abort`,
  no force push, no merge or push of the protected branch.
- An unresolvable review (Pi failure, exhausted review rounds, a
  finding the session could not fix) keeps the PR, branch and worktree
  intact and marks the Issue `ai-blocked` (removing the opened-PR
  state) with the concrete finding.

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
- Pi (the implementer) does not merge and does not push `main` or `master`.
  It delivers through exactly one PR linked to the Issue; the Runner is the
  only merge actor (the Runner is the only merge actor; see below).
- The PR description must contain `Fixes #<issue-number>` (it may be on
  the first line), pointing at the source Issue so GitHub closes the Issue
  natively when the PR merges into the default branch. The keyword works
  in the PR body and in commit messages, but not in the PR title. The
  runner rejects a PR whose body is missing it, so a merge can never
  leave the source Issue open.

## Auto review, fix and merge

- After the implementer opens the PR, the Runner freezes the exact PR
  base/head SHA and runs an independent review session (code-review R1–R9)
  against those SHAs. Since Issue #82 the reviewer is also the fixer: it may
  modify code, run the full suite with 100% line/branch coverage, commit and
  push ONLY the task branch, then re-emit the verdict for the fixed head. The
  reviewer ends with one machine-readable `REVIEW_VERDICT` line; a missing or
  malformed verdict fails fast and is never treated as a pass.
- A `pass` verdict means zero Blocker/Major findings AFTER the in-session
  fixes: the Runner re-freezes the PR (the head may have advanced), runs the
  merge gate against the re-frozen head, and merges with `gh pr merge
  --match-head-commit` so only the reviewed head lands. A finding the session
  could not fix (or a PR behind the latest base / with a merge conflict)
  leaves the head unmergeable: the Issue is marked `ai-fix-needed` and the
  NEXT tick runs the next independent review session on the same PR, which
  absorbs the latest base in-session, resolves conflicts, re-runs the suite
  and re-emits the verdict. The review loop is bounded (5 rounds); if it
  exhausts rounds with findings it fails fast and marks the Issue
  `ai-blocked`.
- The merge gate re-fetches the latest remote base and requires the PR head to
  contain it, the PR to be mergeable, and the remote head to still be the
  reviewed head. A PR behind the latest base is rejected, never
  merged. The Runner confirms the PR is MERGED and the merge commit is on the
  protected branch before marking the Issue `ai-merged`.
- A review finding is not `ai-blocked`: it is fixed in the review session
  (or in the next review session on the same PR). Only command failure, an
  unavailable environment, or a review that cannot be verified fails fast and
  marks `ai-blocked`.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues
  and labels are the only state store.
- No business task timeout. systemd only schedules the tick and owns the
  run lifecycle.
