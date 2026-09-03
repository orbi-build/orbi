# AGENTS.md

Development contract for this repository. Every local Pi bootstrap run
must follow it before changing code. This file carries only the rules
the agent must obey — the operational explanation of every mechanism
(timer, unit drift, transport check, journal fields, label lifecycle,
claim scans) lives in the docs site (`docs/`, <https://docs.orbi.build>);
each section points at the owning page instead of restating it.

- **Implementer Pi**: plan, TDD, tests, commit the delivery; the Runner pushes the task branch and opens one PR.
- **Reviewer Pi**: after the PR exists, a new `pi --print` with `prompt_review.md` and a new JSONL on the same worktree.
- **Runner**: labels, observability, review/fix, and merge.

## Issue granularity

- One Issue is **one runtime outcome** (when X, should Y, actually Z): one observable behavior, a handful of related files, tests included.
- Open the Issue once the root cause is pinned.

## Minimal implementation (KISS/LEAN)

- Implement the smallest complete change that satisfies the Issue's acceptance criteria — no speculative feature, no no-benefit abstraction, no extra framework layer, no fallback, no future-proofing, no scope expansion.
- 如无必要勿增实体: every new file, dependency, state, label, command and abstraction must map to an acceptance criterion.
- When two designs both satisfy the requirements, choose the simpler one: fewer concepts, fewer files.
- The MVP scope stays unchanged: no database, queue, DAG, daemon, risk engine or fallback.

## Read first

- Read the GitHub Issue (body and comments) first. Then, in priority order: the repository `AGENTS.md`, the files you will change plus their callers, and the related tests.
- `README.md`, the configured context files, build files and history are read only when the task is actually about them — a normal Issue never requires a full repository scan, and re-reading the same large files triggers pointless compactions.

## TDD and coverage

- TDD: write a failing test first, then the smallest implementation, then refactor.
- External APIs, CLI flags and HTTP paths are asserted against official docs or one real call.
- Blocking commands (Issue #95): any shell command that can block (running tests, generator/polling verification, network waits, interactive tools) is wrapped in `timeout <seconds> ...`; a timeout is the signal that the path needs a fix, never ignorable noise.
- Testing an unbounded-loop function (`while True` poller) requires a termination guard (monkeypatched `time.sleep` raising on the Nth call, an injected iteration cap, or pytest-timeout): the red phase must fail fast and never hang.
- Test exit codes (Issue #180): a pipeline exits with the exit code of the last command, so `pytest ... | tail` exits 0 even when pytest fails — never pipe a test, build or smoke command through `tail`, `head`, `grep` or any other filter that drops the exit code; redirect to a file and keep the real exit code (`set -o pipefail`, or `> test.log 2>&1; echo "exit=$?"`); `test.log` carries the real pytest output, never a self-declared "tests passed".
- Coverage gate (Issue #234), tiered: the whole repository keeps line >= 95% and branch >= 95% (checked separately, never a merged single percentage); the Python lines and branches changed in the current PR keep 100% line/branch; the core state machines and critical failure paths (model_wait/idle recovery, Issue/label lifecycle, Git/PR/merge gate, config validation, deployment failure paths) keep 100% line/branch through their existing tests. Code below 95% is only allowed for non-core, clearly explainable legacy/defensive branches and must stay locatable in the coverage report — no unjustified `# pragma: no cover`. Full policy: `docs/testing.mdx` (EN) / `docs/zh/testing.mdx` (ZH):

  ```bash
  /usr/bin/python3 -m coverage run --branch -m pytest tests/ -q
  /usr/bin/python3 -m coverage report --show-missing
  /usr/bin/python3 coverage_gate.py
  /usr/bin/python3 diff_coverage_gate.py origin/main
  ```

## UI work

- Any UI task drives the real running app with Playwright: real interaction, an assert on the changed flow, console and network error checks, and a screenshot saved under the run artifacts.

## Fail fast

- Command errors fail fast: log the command, return code, stdout and stderr, then raise. Never swallow an error or add a fallback path.

## Observability (contract)

- Progress is automatic: no human status command, no polling, no supervision; `muyan-pilot status` is a debug attachment only.
- The journal is the record: a heartbeat at most every 30 seconds, every line prefixed `[run_id]`; a stalled session is recovered automatically, and a frozen `model_wait` past `PI_MODEL_WAIT_DEAD_SECONDS` (default 1800 s; the pre-#228 default was 600 s) is a hung model request — the Runner logs `model_wait_dead` (`upstream_alive` is evidence, never a veto) and kills the Pi session. It never fires while events keep arriving: a slow generation is not a hung request, and none of this is a business task timeout.
- GitHub: exactly one progress comment per run, PATCHed in place, with short milestone comments; it is a pure bypass — a `progress_publish_failed` never fails the delivery, never marks the Issue `ai-blocked`, and never skips `run_pi` / `wait_for_delivery`. The `Muyan Pilot opened PR:` scene comment is NOT a bypass: the next tick's resume parses it, so a failure there fails the delivery fail-fast.
- Field reference and full mechanics: `docs/operations.mdx` (EN) / `docs/zh/operations.mdx` (ZH) (the README homepage keeps a one-sentence summary plus the link, Issue #241).

## Base freshness and deployment (contract)

- Every task worktree is created from the frozen `origin/<base_branch>` SHA (default `main`), never from the main worktree's current HEAD; branch and worktree names carry the unique run id.
- The agent stops at the committed delivery. The Runner re-fetches `origin/<base_branch>` under the shared base-sync lock and absorbs an advanced base with a plain `git merge` on the task branch, then pushes and opens the PR. A delivery is acceptable only when its HEAD contains the latest remote base. No auto conflict resolution, no force push, no merge or push of the protected branch.
- The repo templates `systemd/muyan-pilot@.service` and `systemd/muyan-pilot@.timer` are the single source of truth for the installed user units. `muyan-pilot install-units` is the idempotent install (copy, legacy migration, `daemon-reload`, enable the timer instances): it NEVER starts/stops/restarts the service — a running Runner is never killed or restarted by an install.
- A template change is a deployment change: it takes effect without a human step — the next timer trigger's `ExecStartPre` syncs the checkout, and the pre-start drift check self-heals the installed units with the same install (`unit_drift auto_synced` line per unit). A drift the self-heal cannot resolve is caught by the pre-start check: the `unit_drift` line, fail fast — no slot, no claim, no label change. `muyan-pilot doctor` is the read-only report.
- The service `ExecStart` is the installed `muyan-pilot` CLI (an editable `uv tool` install; ordinary source/template changes need no reinstall or upgrade). The runtime code lives in the `src/muyan_pilot/` package (Issue #168 src layout): the editable finder maps the WHOLE package directory, so a newly added package module needs no reinstall (the #158 stale-module-list root cause). A stale or non-editable CLI source is reported by `muyan-pilot doctor` (`cli_source: DRIFT`) and repaired by `muyan-pilot setup`. When the breakage is severe enough that the console script cannot even `import muyan_pilot` (the #248 scene: a src-layout migration left the installed editable finder stale), the service's SECOND `ExecStartPre` step self-heals OUTSIDE Python: it probes `muyan-pilot --version` and, on probe failure, runs the editable force-reinstall under the same `base-sync.lock` flock (the #158 in-Runner refresh is unreachable in that scene). The checkout root carries NO `muyan_pilot.py`: a flat file named like the package would shadow the installed package for every process with the checkout root on sys.path; the direct-execution compatibility entry is `python3 -m muyan_pilot.cli` (development/compatibility path, never the documented usage).
- Mechanics (timer instances, the pre-start preflight sequence, the unit-drift self-heal, the slots): `docs/operations.mdx` (EN) / `docs/zh/operations.mdx` (ZH); the CLI install refresh (`cli_install_failed`) is documented in `docs/getting-started.mdx`.

## Git transport (Issue #114)

- Git data operations (fetch, push — including pushing `.github/workflows/*.yml`) go over SSH (`git@github.com:owner/repo.git`); GitHub API operations (Issue, PR, label, comment, merge) stay on the `gh` token. SSH is never used as API authentication and the `gh` token is never used for git data.
- Pre-start check: the configured `origin` URL is SSH for the first source repo and `git ls-remote <ssh-url>` exits 0; a failure logs `transport_check_failed` and fails the start — no slot, no claim, no label change, no HTTPS fallback, no silent skip.
- An existing HTTPS remote is never rewritten silently: only the human-run `muyan-pilot setup` migrates it with `git remote set-url origin git@github.com:owner/repo.git`; every other path fails fast with the exact migration command. `muyan-pilot doctor` reports the transport read-only.
- Full explanation: `docs/operations.mdx` and `docs/setup.mdx` (EN/ZH).

## Task dependencies (blockedBy)

- Dependencies use GitHub's native `blockedBy` relation (`gh issue edit N --add-blocked-by M`); never write `Depends on #N` in the Issue body — the runner does not parse body dependencies.
- An open blocker means the Issue is not claimed (no `ai-in-progress`, no label change, no worktree); a closed blocker no longer blocks. A failed `blockedBy` query fails open and never deadlocks the queue.
- No DAG, no topological sort, no multi-worker scheduling: the single-slot serial execution only reads the field, skips, and waits.
- Explanation: `docs/workflow.mdx` (EN/ZH).

## Pickup priority (P0)

- Emergency priority is the plain label `p0` — NOT a delivery state: it only orders the ready pickup. The Runner never adds or removes it.
- The ready pickup order is fixed: `ai-ready`+`p0` → `ai-ready`+`bug` → plain `ai-ready` (three scans sharing the exact same exclusions and blockedBy semantics). P0 obeys every existing exclusion rule (`ai-in-progress`, `ai-pr-opened`, `ai-fix-needed`, `ai-merged`, `ai-blocked`) and the single-slot constraint.
- The optional config field `active_milestone` (a Milestone TITLE) restricts the FRESH-claim scans to one version; the value is explicit — never guessed from the repo's Milestone list, and an empty/non-string value fails the start fast.
- The pickup log line and the progress comment carry `priority=p0` / `priority=normal`.
- A failed P0 run enters `ai-blocked` ALONE — no tick re-claims it, so there is no infinite retry.
- Explanation: `docs/workflow.mdx` (EN/ZH); the `active_milestone` field: `docs/getting-started.mdx`.

## Epic Issues (ai-epic)

- An Epic is a coordination Issue that groups related tasks; it carries the plain `ai-epic` label and is NOT an executable task — the work is split into independent `ai-ready` sub-Issues, each with one PR, one independent review and one merge.
- The ready claim scan NEVER claims an `ai-epic` Issue (no `ai-in-progress`, no label change, no worktree, no run, no slot) and the restart-resume scan excludes it too.
- The Runner never marks an Epic complete or closes it: while any completion condition (sub-Issues done, PRs merged, remote tag/artifacts, no leftover `ai-in-progress`) is unmet the Epic stays open.
- Explanation: `docs/workflow.mdx` (EN/ZH).

## Release tasks (ai-release)

- A Release task is an `ai-ready` Issue additionally marked with the plain `ai-release` label: it NEVER enters the `run_pi` path — the Runner's deterministic release state machine delivers it (no Pi session, no PR), idempotent and resumable.
- The `ai-release` label is a type marker, NOT a delivery state: the Runner never adds or removes it — only the human does.
- Success: `ai-merged` (terminal) and the release Issue is closed. Any failure: `ai-blocked` ALONE (a release is a human decision point — no automatic retry).
- The full 9-step state machine contract: `docs/workflow.mdx` (EN) / `docs/zh/workflow.mdx` (ZH).

## Review, fix and merge (same PR)

- The review session is independent (a new Pi process, `prompt_review.md`, a new JSONL) and ALSO the fixer: it may modify code, run the full suite with the tiered coverage gate (Issue #234: whole repository line/branch >= 95% checked separately, changed Python code at 100%), commit, and push ONLY the task branch, then re-emit the `REVIEW_VERDICT` for the fixed head. A `pass` verdict means zero Blocker/Major findings AFTER the in-session fixes; a missing or malformed verdict fails fast and is never treated as a pass.
- `ai-pr-opened` means awaiting review; `ai-fix-needed` marks a delivery whose head is not mergeable yet (a finding the session could not fix, a PR behind the latest base / with a merge conflict, or an AI-recoverable failure of the existing run/PR): the NEXT tick resumes the SAME run_id, branch, worktree and PR and runs the next independent review session, which absorbs the latest base in-session. Never a replacement PR, never a re-claim.
- The merge gate re-fetches the latest remote base and requires the PR head to contain it, the PR to be mergeable, and the remote head to still be the reviewed head; the merge lands exactly that head (`gh pr merge --match-head-commit`).
- The review loop is bounded (5 rounds); exhausting rounds with findings fails fast and marks the Issue `ai-blocked`.
- Chain, state semantics and label lifecycle: `docs/workflow.mdx` (EN/ZH); the recovery scene contract (trusted-maintainer comment, derived branch/worktree, PR body run marker): `docs/security.mdx` + `docs/workflow.mdx` (EN/ZH).

## Run correlation

- One task attempt generates one run_id (8 hex chars) and reuses it for every later step of the attempt; a retry generates a new one. No new id system: no trace_id, no log_id, no second UUID, no tracing backend.
- Every journal line of the attempt starts with `[run_id]`; every Issue/PR comment and the PR body carry the stable marker `<!-- muyan-pilot:run=<run_id> -->` plus the visible `run_id=` field; branch, worktree, Pi session dir and run artifacts carry it in their paths. A run-scoped event without a valid run_id fails fast.
- Explanation: `docs/workflow.mdx` (EN/ZH).

## Git

- Work on the task feature branch.
- Pi (the implementer) does not merge and does not push `main` or `master`. It delivers through exactly one PR linked to the Issue; the Runner is the only merge actor.
- The PR description must contain `Fixes #<issue-number>` (it may be on the first line), pointing at the source Issue so GitHub closes the Issue natively when the PR merges into the default branch. The keyword works in the PR body and in commit messages, but not in the PR title. The runner rejects a PR whose body is missing it.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues and labels are the only state store.
- No business task timeout. systemd only schedules the tick and owns the run lifecycle.
