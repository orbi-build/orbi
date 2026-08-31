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

## Minimal implementation (KISS/LEAN)

The positive contract for every implement and review session (Issue #118):
implement the smallest complete change that satisfies the Issue's
acceptance criteria.

- KISS and LEAN are the default: no speculative feature, no
  no-benefit abstraction, no extra framework layer, no fallback, no
  future-proofing, no scope expansion beyond the Issue's acceptance
  criteria.
- 如无必要勿增实体 — do not multiply entities beyond necessity: every new
  file, dependency, state, label, command and abstraction must map
  to an acceptance criterion of the Issue.
- When two designs both satisfy the requirements, choose the simpler one:
  fewer concepts, fewer files.
- The MVP scope below stays unchanged: no database, queue, DAG, daemon,
  risk engine or fallback.

## Implement vs review

- Implementer session: plan, TDD, tests, commit the delivery; the Runner
  pushes the task branch and opens one PR (Issue #186: the deterministic
  Git/GitHub closeout — base fetch and absorb, push, PR creation — is the
  Runner's job, not the agent's).
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
- Blocking commands (Issue #95): any shell command that can block (running
  tests, generator/polling verification, network waits, interactive tools)
  is wrapped in `timeout <seconds> ...` — a timeout is the signal that the
  path needs a fix, never ignorable noise. Testing an unbounded-loop
  function (`while True` poller) requires a termination guard (monkeypatched
  `time.sleep` raising on the Nth call, an injected iteration cap, or
  pytest-timeout): the red phase must fail fast and never hang.
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
  command, no polling, no supervision. `muyan-pilot status` is a debug
  attachment only — never part of the normal workflow or acceptance
  evidence.
- Journal: while a session runs, the journal gets a heartbeat at most every
  30 seconds and an immediate event on phase/action change. Every line
  carries issue, run id, role (implement/review/merge), phase, elapsed,
  last activity, last action, session and branch. No model/session activity
  for 5 minutes logs an idle warning; the first new activity after it logs
  a resumed event. A stalled (non-`model_wait`) session is recovered
  automatically, not only warned (Issue #94, evidence-based since Issue
  #169): one step per idle window of `PI_IDLE_WARN_SECONDS` since the
  stall was first seen — window 1 checks the Pi descendants that already
  existed before the window (the hung tools: ppid chain + start time from
  `/proc/<pid>/stat`, never a name guess; only Pi descendants, never
  other system processes, never a process spawned after the window began,
  never a zombie that has already exited). If one of them is a coreutils
  `timeout <seconds> ...` command still inside its deadline (start time +
  duration, computed in the monotonic clock domain so NTP realtime steps
  cannot skew it), it is a legitimate long-running command, not a hang:
  the Runner logs one `pi_idle_wait` line (run id, pid, cmdline,
  deadline), reports `recovery=wait` in the progress comment, and pauses
  the escalation — every later window re-evaluates. The nominal deadline
  is BEST-EFFORT (Issue #181): the wrapper's own deadline handling
  (alarm → signal delivery → exit) is best-effort and can be delayed by
  scheduling, so a single "past deadline and still alive" observation is
  not evidence the wrapper failed. The grace is measured in idle windows,
  not polls: a target first observed past its deadline in cycle N is
  recorded (the grace window — nothing is signaled, `recovery=wait` stays
  visible) and signaled only if it is STILL alive in a LATER window (one
  full idle window of grace, cycle > N) — that is when the wrapper failed
  to end the command and the evidence flips and the escalation proceeds.
  Otherwise
  window 1 SIGTERMs the descendants, so the tool gets a non-zero exit and
  the failure signal reaches the model; window 2 SIGKILLs a target that
  survived; after `PI_IDLE_RECOVERY_CYCLES` (default 3) consecutive idle
  windows the Runner kills the Pi session itself and fails fast through
  the normal failure path (the slot is never held forever). Every step
  logs a `pi_idle_wait` / `pi_idle_term` / `pi_idle_kill` line (run id,
  pid, cmdline, deadline/TERM/KILL, result) and the progress comment shows
  the recovery state via its `recovery` field (`wait` / `term` / `kill`);
  the first new activity resets the whole recovery state. A session frozen
  in `model_wait` (the newest event is a tool result) past
  `PI_MODEL_WAIT_DEAD_SECONDS` (default 600 s) is a HUNG model request
  (Issue #75, safe recovery since Issue #218): the model service process
  being alive and the Pi connection to it still ESTABLISHED (a
  `socket:[...]` inode of the Pi process in `/proc/net/tcp` or
  `/proc/net/tcp6` in state ESTABLISHED, SYN_SENT or SYN_RECV with a
  non-zero remote address) are NOT an exemption — process alive ≠
  responding (the #183 scene: llama-server alive, `/health` ok, the
  request hung, the slot held for hours). The Runner logs one structured
  `model_wait_dead issue=... role=... idle_seconds=... threshold=...
  action=kill_pi session=... run_id=... upstream_alive=true|false
  reason=hung_model_request` line (`upstream_alive` is evidence for the
  journal, never a veto), kills the Pi session, and fails fast through
  the normal failure path with
  `run_failed ... reason=model_wait_dead_stale_...` (the Issue is marked
  `ai-blocked` in the implement phase and `ai-fix-needed` in the review
  phase, with the scene in the Issue comment; the slot is released by the
  kernel when the process exits, and the next tick resumes the same
  run/branch/worktree/PR or claims the next `ai-fix-needed`). It never
  fires while events keep arriving (a slow generation is not a hung
  request) and it is NOT a business task timeout.
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
- The Runner re-fetches `origin/<base_branch>` before creating the PR
  (Issue #186: the agent stops at the committed delivery; the base fetch
  and absorb, the push and the PR creation are the Runner's deterministic
  closeout); if the base advanced, the Runner merges it into the task
  branch with a plain `git merge` (a conflict is aborted and the PR opens
  on the agent's head), then pushes the task branch (the independent review
  runs after the PR is opened and absorbs any further base advance
  in-session).
- Every fetch that updates the shared remote-tracking ref
  (`refs/remotes/origin/<base_branch>`) runs under the SAME shared
  state-dir lock (`base-sync.lock`) that `ExecStartPre` uses (Issue #171):
  the Runner-side `freeze_base` / `verify_pr` / `merge_gate` /
  `confirm_merged` fetches go through `fetch_base_ref` (which acquires the
  lock, fetches, and releases it), and the implement/review prompts
  instruct Pi to run the base-freshness fetch as
  `flock <BASE_SYNC_LOCK> git fetch origin <base_branch>` (the lock path is
  rendered into both prompts from the configured `repo_dir`). Two concurrent
  Runners (or a Runner and a Pi session) therefore never race the shared
  ref — no `cannot lock ref ... is at <X> but expected <Y>` failures — and
  a lock or fetch error fails fast with the concrete error, never a retry
  or a fallback fetch.
- The runner rejects a delivery whose HEAD does not contain the latest remote
  base. No auto conflict resolution, no force push, no merge or push of the
  protected branch.
- A delivery is acceptable only when its HEAD contains the latest remote base;
  base updates use a plain `git merge` on the task branch.
- The Runner's own code updates at the next service start (Issue #52):
  the service template `muyan-pilot@.service` (deployed as the two
  instances `muyan-pilot@1.service` / `muyan-pilot@2.service`) runs
  `ExecStartPre` = the `git fetch origin main && git merge --ff-only
  origin/main` in the main checkout wrapped in a short-lived `flock` on
  the shared state-dir lock file (`base-sync.lock`, the Python-side
  sync takes the SAME lock) before `ExecStart`. A dirty checkout, a
  failed fetch or a non-fast-forwardable state fails the preflight: the
  service does not start and the reason lands in the systemd journal
  (fail fast). A currently running long task is never hot-updated or
  killed — while one service instance is active, systemd ignores
  further starts of THAT instance, and the next real start runs the
  latest code. Two instances may run concurrently; the capacity is the
  Runner's flock slots (`max_concurrency`), never the instance count.
  No refresh service, worker, dispatcher or resident process is added;
  the 5-minute timers are unchanged.
- Deployment consistency (Issue #103, #142): the repo templates
  `systemd/muyan-pilot@.service` and `systemd/muyan-pilot@.timer` are the
  single source of truth for the installed user units. The idempotent
  install is `muyan-pilot install-units` (copy both templates into the
  user unit dir, migrate the pre-#149 non-templated units away once —
  `systemctl --user disable --now muyan-pilot.timer` (a timer stop, never
  the service) plus removing the legacy files — `systemctl --user
  daemon-reload`, enable the two timer instances, print the deployed
  commit and unit hashes): it NEVER starts/stops/restarts the
  service — a running Runner is never killed or restarted by an install,
  the new config takes effect at the next service start. Every Runner start
  checks BOTH installed units against the templates BEFORE any slot or
  claim: drift is self-healed with the SAME idempotent install (the
  pre-start sync, Issue #142) and re-verified with the SAME hash check —
  clean logs one structured `unit_drift auto_synced` line per unit (before
  and after sha256, deployed commit) and the start continues. Drift that
  survives the sync — or a failing install step — logs a structured
  `unit_drift` line per unit (repo path, installed path, sha256s, the
  install fix command) and fails fast — no slot, no claim, no label
  change. The read-only report is `muyan-pilot doctor` (repo commit,
  unit drift, timer/service active, slots, Pi session, current Issue,
  recent journal). Full sequence:
  merge to main -> timer next trigger -> ExecStartPre syncs origin/main ->
  drift check (auto-sync + re-verify when drifted) -> Runner starts one
  Issue.
- The service `ExecStart` is the installed `muyan-pilot` CLI (Issue #140,
  #152): the official usage is the EDITABLE `uv tool`-installed console
  script (`uv tool install --force --reinstall --editable --python
  /usr/bin/python3 <deployment checkout>` — the tool env imports
  `muyan_pilot` from the deployment checkout, so the ExecStartPre
  checkout sync is picked up by the next CLI process automatically and
  ordinary source/template changes need NO reinstall or upgrade), and
  the unit uses the explicit deployable entry
  `%h/.local/bin/muyan-pilot` (verifiable with
  `systemd-analyze --user verify`); the direct-execution entry of
  `muyan_pilot.py` stays a development/compatibility path, never the
  documented usage. A non-editable (site-packages) or stale CLI source
  is reported by `muyan-pilot doctor` as `cli_source: DRIFT` with the
  structured `cli_source_drift` line (actual import path, expected
  repo_dir, the exact editable reinstall command) and repaired by
  `muyan-pilot setup` (the CLI step verifies or force-reinstalls the
  editable install).
- The Runner refreshes the editable install at start (Issue #158):
  the editable finder's module MAPPING is generated at INSTALL time
  from the checkout's `pyproject.toml` (`py-modules`, entry points,
  version, dependencies), so a merged PACKAGING change (e.g. a new
  runtime module in `py-modules`) leaves a stale finder and the next
  CLI process dies with `ModuleNotFoundError` before the Runner can
  start (the #158 incident: `cli_source` merged to main, the installed
  finder still mapped the pre-#152 module set, the systemd start
  failed). The Runner tick entry (BEFORE any slot or claim; the CLI
  subcommands never install) compares the packaging fingerprint (the
  sha256 of the checkout's `pyproject.toml`) with the last successful
  install's fingerprint, stored in the shared state dir
  (`<repo_dir>/.muyan-pilot/cli-install.json` — the same gitignored
  dir as `base-sync.lock` and the slots; it is NOT a second release
  state): unchanged -> NO uv call at all (no per-tick unconditional
  reinstall); changed or no state yet (first install) -> ONE
  lock-protected `uv tool install --force --reinstall --editable
  --python /usr/bin/python3 <repo_dir>` (the SAME base-sync flock the
  service template's `ExecStartPre` uses — two instances starting in
  the same tick serialize, the second re-reads the state under the
  lock and reuses the first's result, never a concurrent uv install),
  the fingerprint is recorded only after a successful install, and a
  failing install fails the start fast with the structured
  `cli_install_failed` line (reason + the exact fix command): no
  slot, no claim, no label change, no state recorded (the next start
  retries). Ordinary Python source content is NOT part of the
  fingerprint (the editable finder maps the live files — a content
  change needs no reinstall), and systemd template changes stay with
  the unit drift mechanism above. The refresh IMPLEMENTATION lives in
  `bootstrap_runner` itself, NOT in a separate new module: the
  bootstrap chain (`muyan_pilot` -> `bootstrap_runner`) must still
  LOAD and REFRESH in a tool env whose installed finder predates this
  PR's packaging change (the #158 scene) — a new module for the
  refresh would not be importable there, and the very refresh that
  reinstalls the tool env could never run (the #158 incident, one
  module later). `cli_install` is a thin re-export of the
  implementation for the tests' single import point; the bootstrap
  chain never imports it (a regression test pins this: a fresh
  `bootstrap_runner` loads with `cli_install` blocked from the import
  system and its `refresh_cli_install` gate still runs).
- A template change is a deployment change (Issue #131, #142): a PR
  that modifies `systemd/muyan-pilot@.service` or `systemd/muyan-pilot@.timer`
  takes effect without a human step — the NEXT timer trigger's
  `ExecStartPre` syncs the checkout, and the pre-start drift check
  self-heals the installed units with the same idempotent install
  (copy, legacy migration, daemon-reload, enable the two timer
  instances — never touches a running Runner) before the tick continues. No per-tick drift loop until human
  intervention (the #131/#140 scene). `muyan-pilot install-units` stays
  the manual entry (setup, immediate sync); a drift the self-heal cannot
  resolve is still caught by the pre-start check (structured
  `unit_drift`, fail fast, no slot, no claim) — the gate stays the
  canary for the deployment, not a bug to be bypassed.

## Git transport (Issue #114)

- Two authentication channels with distinct responsibilities: **Git
  data operations** (fetch, push — including pushing
  `.github/workflows/*.yml`) go over **SSH**
  (`git@github.com:owner/repo.git`, the machine's SSH key); **GitHub
  API operations** (Issue, PR, label, comment, merge) stay on the
  existing `gh` token. SSH is never used as API authentication and the
  `gh` token is never used for git data. A workflow push must never
  depend on the OAuth App `workflow` scope (the HTTPS/OAuth transport
  that blocked Issue #106).
- The deployment checkout's single `origin` remote is the transport:
  a task worktree created with `git worktree add` shares the main
  repository's remote configuration (verified against real git), so
  the transport is configured once on the checkout and every worktree
  inherits it. New bootstrap worktrees therefore have an SSH
  `git remote -v` by construction.
- Pre-start check: BEFORE any slot or claim (right after the unit-drift
  preflight) the Runner verifies the checkout's transport — the
  CONFIGURED `origin` URL (`git config remote.origin.url`, never the
  insteadOf-rewritten data-plane URL) is SSH for the first configured
  source repo, and `git ls-remote <ssh-url>` exits 0 (SSH reachable and
  authenticated — verified against the real CLI). A failure logs the
  structured `transport_check_failed ... reason=...` line and fails the
  start: no slot, no claim, no label change, **no HTTPS fallback, no
  silent skip**.
- An existing HTTPS remote is never rewritten silently and never read
  from a comment or Issue body: only the human-run setup entry
  (`muyan-pilot setup`) migrates it with the plain
  `git remote set-url origin git@github.com:owner/repo.git`; every
  other path fails fast with the exact migration command. A remote
  that does not point at the first configured source repo is never
  migrated (the rewrite would re-target the checkout at a different
  repository) — it fails with the mismatch scene whether or not the
  setup entry authorizes the migration. `muyan-pilot doctor` reports
  the transport read-only (protocol, expected URL, SSH probe) — a
  failed transport is REPORTED there, not raised (the pre-start check
  is the fail-fast gate).

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

## Pickup priority (P0)

- Emergency priority is the plain GitHub label `p0` — NOT a delivery
  state: it only orders the ready pickup, never changes the Issue
  granularity (one Issue = one runtime outcome), any delivery state, or
  the terminal-state semantics. The Runner never adds or removes it.
- The ready pickup order is fixed: `ai-ready`+`p0` → `ai-ready`+`bug`
  → plain `ai-ready` (three `gh issue list` scans sharing the exact
  same exclusions and blockedBy semantics). P0 obeys every existing
  exclusion rule (`ai-in-progress`, `ai-pr-opened`, `ai-fix-needed`,
  `ai-merged`, `ai-blocked`) and the single-slot constraint: a blocked
  P0 is skipped (falling back to the bug/plain scans) and an in-flight
  P0 is resumed by the restart scan (which fetches `labels` too, so the
  progress comment keeps showing `p0`).
- Active Milestone claim scope (Issue #139): the optional config field
  `active_milestone` (a Milestone TITLE, e.g. `v0.2.0`) restricts the
  FRESH-claim scans to one version: with it set, all three ready scans
  carry the `milestone:"<title>"` qualifier in the gh search query
  (the quoted form — milestone titles may contain spaces or special
  characters), so an Issue of another Milestone or of no Milestone
  never enters the queue, and a Milestone Issue without `ai-ready`
  never does either. The Milestone is a version scope, NOT a
  replacement for the `ai-ready` execution switch. P0 does NOT cross
  milestones (the active Milestone is the claim scope of every fresh
  claim; `p0` only orders the pickup inside it). The scope is
  query-layer only (the `is_epic` code-layer skip and the blockedBy
  skip are the unchanged second layer), a failed scan still fails open
  (never a silent claim of the wrong version), and resume states are
  never gated by it: the opened-PR resume and the in-flight restart
  scans run work to completion regardless of Milestone changes. The
  value is explicit — never guessed from the repo's Milestone list;
  absent it keeps the pre-#139 behavior exactly (compat), and an
  empty/non-string value fails the start fast.
- The pickup log line carries the explicit `priority=p0` /
  `priority=normal` field; the GitHub progress comment shows the
  `priority` field; the run scene (`run_info`) and the started
  milestone carry `priority=...`.
- A failed P0 run enters `ai-blocked` ALONE (the claim label is
  removed; the `ai-ready` residue is excluded by every ready scan) —
  no tick re-claims it, so there is no infinite retry; the failure
  comment and blocked scene keep the concrete reason and the
  recoverable scene.
- review/merge failures of a P0 PR follow the existing same-PR, bounded
  review-round mechanism (no new loop).

## Epic Issues (ai-epic)

- An Epic is a coordination Issue that groups related tasks (a release
  checklist, a multi-task grouping). It carries the plain `ai-epic`
  label and is NOT an executable task: the actual work is split into
  independent `ai-ready` sub-Issues, each with one runtime outcome, one
  PR, one independent review and one merge. Preconditions between
  sub-Issues use the native `blockedBy` relation (see Task
  dependencies) — the Runner never parses body checkboxes or
  `Depends on` lines to infer dependencies.
- The ready claim scan NEVER claims an `ai-epic` Issue (Issue #93): no
  `ai-in-progress`, no label change, no worktree, no run, no slot — a
  structured `epic_not_claimed issue=N repo=...` line is written and
  the next ready Issue is considered. The Epic check precedes the
  blockedBy check: "it is an Epic" is the recorded cause, never a
  `blocked_by` line. The restart-resume scan excludes `ai-epic` too:
  a legacy Epic left behind with `ai-in-progress` (the #80 scene,
  before the Epic mechanism existed) is never resumed into a run.
- The Epic's completion is judged from GitHub evidence — sub-Issues
  done, their PRs merged, the release tag/artifacts on the remote, no
  leftover `ai-in-progress` — and the Epic is closed by a human or a
  release task (an `ai-ready`+`ai-release` Issue, see Release tasks
  below). The Runner never marks an Epic complete or closes it: while
  any completion condition is unmet the Epic stays open. No database,
  queue or resident service tracks the Epic — GitHub Issues/labels,
  native `blockedBy`, PRs and the remote tag are the only state.

## Release tasks (ai-release)

- A Release task is an `ai-ready` Issue additionally marked with the
  plain `ai-release` label (Issue #98). It is NOT a development task
  and NEVER enters the `run_pi` path: the ready scan picks it up like
  any task, but `process_issue` routes it to the Runner's deterministic
  release state machine — no Pi session, no PR. The three task types
  stay distinct: an Epic (`ai-epic`) is coordination only and never
  claimed; a normal task (`ai-ready`) delivers through `run_pi` → PR
  → review → merge; a Release task (`ai-ready`+`ai-release`) delivers
  through the release state machine.
- The state machine is idempotent and resumable (a restart resumes the
  same run id/worktree, the same resume rule as normal tasks) and runs
  these steps in order:
  1. Strictly parse the `## Release` declaration from the Issue body:
     `version`, `base_branch`, `test_command`, `scope` (a list of
     `#N` items). Missing, duplicated or unknown fields fail fast.
  2. Freeze the base — the release commit is exactly
     `origin/<base_branch>` (fetched under the base-sync lock).
  3. Enforce the pre-release gates: no leftover open
     `ai-in-progress` / `ai-pr-opened` / `ai-fix-needed` Issues (the
     release Issue itself excluded), CI green on the release commit
     (no check runs passes with explicit evidence), and no open PRs
     targeting the base branch. A gate that cannot be checked because
     of a real `gh` failure is a failed gate.
  4. Verify the scope item by item: `gh pr view` must report `MERGED`,
     otherwise `gh issue view` must report `CLOSED`. Checkbox parsing
     is forbidden — every scope item is checked against the live API.
  5. Run the declared `test_command` in a clean worktree at the
     release commit (`timeout`-wrapped, Issue #95).
  6. Tag: when the remote tag is absent, create an annotated tag at
     the release commit and push it with a plain push (never
     `--force`). When it exists it must point EXACTLY at the release
     commit — a mismatch fails fast. An existing tag is never moved,
     overwritten or force-pushed.
  7. Publish the GitHub Release (idempotent) whose notes carry the
     full verification evidence: version, tag, release commit,
     per-item scope evidence, gate evidence, test evidence and the run
     marker.
  8. Success: `ai-merged` (terminal) and the release Issue is closed,
     with the success comment (release URL, tag, commit, evidence).
     Any failure: `ai-blocked` ALONE (a release is a human decision
     point — no automatic retry), the failure comment carries the run
     marker and the concrete reason, and the exception propagates so
     the tick fails fast.
- The `ai-release` label is a type marker, NOT a delivery state: it is
  not part of the delivery-state machine, not an exclusion of the
  ready scan, and the Runner never adds or removes it — only the
  human does (at dispatch time, together with `ai-ready`).

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
  the session could not fix, the PR is behind the latest base / has a
  merge conflict, or an AI-recoverable failure of the existing run/PR
  — a Pi execution failure, a verification failure, an unpushed local
  commit (the #158 scene: the local commit is preserved and the next
  review session pushes the task branch on the same PR), or a missing
  worktree — Issue #50): the NEXT tick resumes the SAME run_id, branch,
  worktree and PR and runs the next independent review session, which
  merges the latest `origin/<base>` into the branch IN-SESSION, resolves
  any conflict, re-runs the full suite and re-emits the verdict. A
  recoverable failure is reported on the Issue AND the PR with the full
  scene (run_id, PR, branch, worktree, session, phase, last activity and
  the concrete error). Both opened-PR states are scanned (Issue #70).
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
- A RECOVERABLE failure of the existing run/PR (Issue #50: a Pi
  execution failure, a verification failure, an unpushed local commit,
  a missing worktree, a finding the session could not fix) keeps the PR,
  branch and worktree intact and marks the Issue `ai-fix-needed` (the
  opened-PR state label is removed) with the failure comment on the
  Issue AND the PR carrying the full scene (run_id, PR, branch,
  worktree, session, phase, last activity, concrete error); the next
  tick resumes the same run, branch, worktree and PR — no replacement
  PR, no re-claim, never `ai-blocked`.
- An UNRECOVERABLE failure (Issue #50: an external precondition the AI
  cannot safely judge or fix — an unrecoverable scene, a base-branch
  config change, an exhausted review round budget, a PR closed without
  merge) keeps the PR, branch and worktree intact and marks the Issue
  `ai-blocked` ALONE (the opened-PR state label is removed, and a
  leftover `ai-fix-needed` too) with the explicit reason why automatic
  recovery is impossible.

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
- A review finding or an AI-recoverable failure of the existing run/PR
  (Issue #50: a Pi execution failure, a verification failure, an unpushed
  local commit, a missing worktree) is NOT `ai-blocked`: it is fixed in
  the next review session on the same PR (the Issue stays `ai-fix-needed`
  and the next tick resumes the same run, branch, worktree and PR).
  Only an unrecoverable external precondition (an exhausted review round
  budget, an unrecoverable scene, a base-branch config change, a PR
  closed without merge) fails fast and marks `ai-blocked` — and the
  blocked comment must state the explicit reason why automatic recovery
  is impossible.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues
  and labels are the only state store.
- No business task timeout. systemd only schedules the tick and owns the
  run lifecycle.
