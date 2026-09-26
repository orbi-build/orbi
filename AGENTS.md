# AGENTS.md

Development contract for this repository. Every local Pi bootstrap run
must follow it before changing code. This file is self-contained: every
rule an agent must obey to deliver a change here is stated below,
including the failure that motivated it, so a delivery never depends on
chasing a reference first. The deeper operational explanation of each
mechanism (timer instances, unit drift, transport check, journal fields,
label lifecycle, claim scans) lives in the docs site (`docs/`,
<https://docs.orbi.build>) and is linked at the end of the owning section
for when you need the full picture.
Architectural invariants and the public surface live in [CONSTITUTION.md](CONSTITUTION.md); when this file and the constitution disagree, the constitution wins.

**Documentation is not the source of truth — and neither is the code.** This file,
`docs/`, the README and every comment describe what the code is meant to do; the
code describes only what it currently does, which may itself be the bug. Treating
either one as automatically correct turns a defect into a specification.

So when a document and the code disagree, the required action is to **raise it**,
not to pick a side: say which document, which file and line, what the code actually
does, and what the document claims — in the Issue or PR, or as its own Issue.
A human decides which side is wrong. Never silently follow one and leave the
contradiction in place for the next delivery to rediscover.

- **Implementer Pi**: plan, TDD, tests, commit the delivery; the Runner pushes the task branch and opens one PR.
- **Reviewer Pi**: after the PR exists, a new `pi --print` with `prompts/prompt_review.md` and a new JSONL on the same worktree.
- **Runner**: labels, observability, review/fix, and merge.
- **Maintainer**: opens the Issue, decides, verifies what the sandbox cannot,
  merges. **Never writes the delivery's code.** An Issue carrying `ai-ready`
  belongs to the Implementer from that moment: pushing the fix yourself while a
  run is live hands the agent a base with nothing left to change, and the
  delivery ends `ai-blocked` on `the agent delivered no commit on the task
  branch` — the ticket looks failed when the work was in fact done by the wrong
  actor (orbi-website#231, 2026-09-19). Want it done by hand instead? Remove
  `ai-ready` first, so no run is claimed against it.

  What the maintainer does do by hand: deployment and secrets (`wrangler
  secret put`, environment approval), production verification that needs real
  credentials or a browser, and evidence the sandbox cannot produce — all of it
  posted back onto the ticket so the Implementer's acceptance stays inside what
  the sandbox can reach.

  **Writing an Issue: every path and command in it must work inside the
  sandbox.** The delivery runs in a worktree sandbox, not on the machine that
  filed the ticket. A host-only path (`~/Videos/...`, `~/Downloads/...`,
  `/home/<someone>/...`), a host credential, or a step needing a real external
  account makes the run burn a full cycle and end `ai-blocked` with zero
  commits — a defect in the ticket, not in the delivery.

  Go through the acceptance criteria line by line and ask: **what does the
  agent in the sandbox use to do this step?** No answer means the step belongs
  to the maintainer — mark it "maintainer supplies this; not part of the
  delivery's acceptance" and post the evidence yourself. When the ticket points
  at an external asset, **run the fetch command on the runner host first** and
  write the verified command plus the resulting parameters into the ticket; an
  untested command is no better than no command at all.

  orbi-website#325 failed this way twice in one day (2026-09-21): the ticket
  named the video master at a host-only path, the agent could not reach it,
  produced a plan and no commit, and the ticket body was never corrected
  between the two runs.

## The release ticket waits for its milestone on its own

While the milestone still has any other open Issue, the release ticket is **not
claimed at all** — the engine skips it with `release_milestone_incomplete` and
leaves it `ai-ready`. That is a recoverable wait, not a failure.

So to land a ticket in a given version, **add it to that milestone and label it
`ai-ready`** — nothing else. Do not stop the timer, strip the release ticket's
labels, or clear worktrees: that only creates orphaned state to clean up later.

The one exception is a release that already passed its gates (the ticket shows
`release gates passed` / `scope verified`): the scope is frozen by then, so a
newly added ticket belongs in the next milestone.


## Product positioning

- Orbi is a **software dark factory**: an autonomous software delivery system
  for GitHub that takes an Issue through analysis, implementation, testing,
  PR creation, independent review, fixes, merge, and release.
- Public descriptions must not reduce Orbi to Copilot, a chatbot, or a
  development agent. The value proposition is autonomous, end-to-end,
  observable, and recoverable software production: people define goals and
  acceptance criteria; the factory turns them into runnable, verifiable,
  deliverable software.

## Issue granularity

- One Issue is **one runtime outcome**, in one of two shapes: a **fix** (when X, should Y, actually Z) or a **change** (when X, the user should be able to Y; today they cannot because Z) — one observable behavior, a handful of related files, tests included.
- Open the Issue once the root cause is pinned.

## Outbound links carry their origin

Every link to `orbi.build` that leaves this project — README, docs pages,
release notes, Issue and PR comments, outreach copy, a social post — carries
`?ref=<token>` naming **where it was published**. Without it the signup is filed
under the referer host, and that host is the same string for every position on
a site: a README CTA, an issue link and someone's star list all arrive as
`github.com`, and every tweet arrives as `t.co` (X rewrites outbound links).
Telegram, mail clients and a pasted URL send no referer at all and land as
`direct`.

The token is one flat lowercase field (`^[a-z0-9_-]{1,32}$`), layered by prefix
rather than by extra parameters:

```
gh-readme        the repository README's CTA
gh-issue         a link inside an Issue or Discussion
docs-<page>      a docs.orbi.build page, e.g. docs-getting-started
x-<yymmddhhmm>   an X post, to the minute — dozens ship per day, so a date
                 alone collides; the clock is unique without anyone counting
hn-<postid>      Hacker News
tg               the Telegram group
email-<batch>    an email batch
plugin-listing   the homepage link on the claude.com plugin listing page
plugin-readme    a link inside the Claude plugin's README
plugin-skill     the hand-off hint the Claude plugin shows for a repo not yet on Orbi
```

The channel is read back with `substr`, so the schema never grows a column:

```sql
SELECT CASE WHEN instr(source,'-')>0
            THEN substr(source,1,instr(source,'-')-1)
            ELSE source END AS channel,
       COUNT(*) FROM tenants GROUP BY channel;
```

**Links that must NOT carry it**: anything internal to a running session — the
login bounce, status-page navigation, a redirect back from OAuth. A `ref` there
overwrites the visitor's real first touch with a word describing our own
plumbing, which is exactly the defect Issue #716 repaired (`source='webhook'`
had claimed two paying tenants' rows).

No UTM triple: we do not buy ads, so `utm_medium` and `utm_campaign` would be
permanently empty columns. One field, read by prefix.

## Language contract

New public delivery text is written for the project's target users, who are not
limited to the Chinese-speaking world. The public ledger should be directly
readable by HN readers:

- Issue titles are in English.
- PR titles are in English.
- Progress comments are in English.
- Release notes are in English.

Issue bodies put English first; a Chinese section may follow when useful. This
contract applies to newly created Issues, PRs, comments, and releases only;
existing content is not translated. The contract is guidance enforced by this
file, the Issue templates, and the Runner's own output—not a CI gate.

## Minimal implementation (KISS/LEAN)

- Implement the smallest complete change that satisfies the Issue's acceptance criteria — no speculative feature, no no-benefit abstraction, no extra framework layer, no fallback, no future-proofing, no scope expansion.
- 如无必要勿增实体: every new file, dependency, state, label, command and abstraction must map to an acceptance criterion.
- When two designs both satisfy the requirements, choose the simpler one: fewer concepts, fewer files.
- The MVP scope stays unchanged: no database, queue, DAG, daemon, risk engine or fallback.

## Before filing: could this be deliberate?

When something looks wrong, first check whether it is a product decision, a
third party's documented behavior, or a limitation of the tool you are using.
**If you cannot establish which, ask a human — do not file it as a bug.**
A ticket filed on a false premise makes the delivery agent change what should
not change, and burns review rounds.

**Waiting is not breakage.** The runner is built to stop and let a human
decide. Each state below is the design working, and none of them is a
ticket on its own:

| State | What it means |
|---|---|
| `active_milestone` names a closed milestone, or none | The version shipped. Nobody has decided what the next one carries. |
| No claimable Issue; every tick reports `no_ready_issue` | The queue is empty, or what is left is already in flight. |
| A unit sits `inactive dead` with `ExecMainStatus=0` | A oneshot tick finished. The timer starts the next one. |
| `auto_next_milestone = false` and the milestone advance stalls | It is doing exactly what the flag asks — Issue #933 set it on purpose. |
| A delivery ends `ai-blocked` | A terminal state a human is meant to resolve, not a crash. |

These look identical to a fault: nothing moves and nothing complains. Before
writing "stalled", "stuck" or "silently stops" into a ticket, name the
decision the system is waiting for and who owes it. If you can name it, the
gap — if any — is that the person was never told they are being waited on,
or has no way to answer. **File that, not the waiting.**

## No Issue in hand? File one — do not edit

**This repository is delivered by Orbi.** Changes come from a ticket that a
delivery agent executes. They do not come from whoever happens to walk in.

Check this before you touch anything:

| What you have | What to do |
|---|---|
| An Issue assigned to you | Deliver it; carry on to `Read first` |
| "X looks bad" / "X is broken" / "change X" from a person | **File an Issue, then stop** |
| A problem you spotted yourself | **Ask the maintainer first** — file only after they agree (exception below) |

**New Issues need the maintainer's approval, in every orbi-build repository.**
Before filing, send the maintainer the repository, the problem and the smallest
fix, and wait for a yes. The only exception is a bug you reproduced yourself
while testing and are 100% sure of: file it, then tell the maintainer. Never
file "preventive" tickets for incidents that might recur or for theoretical
risks (new probes, gates, self-healing layers, retry budgets, dashboards). One
maintainer with no paying users: fix what a user actually hit, the smallest way.

**When deciding how a feature should behave, one test is growth.** Pick the
option that gets a new user to their first result with the fewest steps and no
dead end. A new user who labels an Issue `ai-ready` and sees nothing happen
leaves; an option that needs them to understand milestones, versions or setup
first loses to one that just works and lets them opt into the details later.

The test is **whether this repo is on Orbi**, not how small the change is or
whether you know how to make it. Being able to make it is not a reason to.

**"It's only a doc" is the hole to close.** What you may edit directly is plain
prose only: `.md` files under `docs/`, the README, this file, ticket bodies and
comments. **Everything else is a ticket**, including:

- Source under `src/`, prompts under `prompts/` — that is code
- Build scripts, CI config, tests — that is code
- "It's one line", "just a rename", "while I'm here" — still code

**What the person filing should bring**: evidence. The command and its real
output, the root cause down to a file and line, a reproduction someone else can
run. That saves the delivery agent from establishing it again and gives the
acceptance criteria something to check against.

**Once it is filed, stop.** After `ai-ready` goes on, that ticket belongs to the
delivery agent. Pushing your own fix leaves it with a base that has nothing left
to change, and the delivery ends with
`the agent delivered no commit on the task branch` — the ticket reads as failed
when the work was simply done by the wrong party.

## Read first

- Read the GitHub Issue (body and comments) first. Then, in priority order: the repository `AGENTS.md`, the files you will change plus their callers, and the related tests.
- `README.md`, the configured context files, build files and history are read only when the task is actually about them — a normal Issue never requires a full repository scan, and re-reading the same large files triggers pointless compactions.

## User-centered acceptance

Every Issue and delivery describes one **User outcome**, its **Preconditions**, the
**Acceptance** path, and the **Evidence** that a user can inspect. The acceptance
follows the user journey: user command and configuration, the system action, the
success result the user sees in the success path, and the failure path (the concrete error the user
sees plus the repair action). Unit tests prove local logic only; they do not
replace a real user path through the affected entry point. Evidence belongs in
the test output, journal, Issue/PR, or UI as appropriate.

Match verification depth to impact rather than running an unrelated full business
journey: documentation changes use the rendered docs page/preview and link checks,
provider changes use a real provider/configuration check, concurrency changes use
contention, setup changes use the install/setup entry point, UI changes use a real
browser flow, and pure internal refactors use the existing public caller and
regression tests. Both success and failure paths need
observable acceptance evidence. Do not add a database, queue, daemon, or state
system merely to make an end-to-end claim.

### Deployment and install deliveries: four checks, none optional

Set by the maintainer on 2026-09-17 after the Docker release broke three times in
one day — each break was "verified something other than what shipped", while the
docs still described a hand-edited provider file.

1. **The implementation runs.** A clean environment reaches the target outcome
   (for Docker: fresh volumes + the published image + Issue through merge).
2. **The docs are correct.** Every variable name, volume name and command matches
   the code, with a test that pins the two sets together.
3. **The docs are executable.** Commands copied from the docs run as written,
   placeholders aside — no undocumented manual step.
4. **The docs are complete.** Injected variables (token, model key, provider),
   volumes (named volumes, bind-mount uid rules), repository mapping, and an Orbi
   quick guide (how to write an Issue, the `ai-ready` label, where to read logs,
   expect a PR within two ticks, the release ticket). The target reader knows
   Docker and nothing about Orbi.

## TDD and coverage

- TDD: write a failing test first, then the smallest implementation, then refactor.
- External APIs, CLI flags and HTTP paths are asserted against official docs or one real call.
- Blocking commands (Issue #95): any shell command that can block (running tests, generator/polling verification, network waits, interactive tools) is wrapped in `timeout <seconds> ...`; a timeout is the signal that the path needs a fix, never ignorable noise.
- Testing an unbounded-loop function (`while True` poller) requires a termination guard (monkeypatched `time.sleep` raising on the Nth call, an injected iteration cap, or pytest-timeout): the red phase must fail fast and never hang.
- Test exit codes (Issue #180): a pipeline exits with the exit code of the last command, so `pytest ... | tail` exits 0 even when pytest fails — never pipe a test, build or smoke command through `tail`, `head`, `grep` or any other filter that drops the exit code; redirect to a file and keep the real exit code (`set -o pipefail`, or `> .orbi/test.log 2>&1; echo "exit=$?"`); `.orbi/test.log` carries the real pytest output, never a self-declared "tests passed".
- Run artifacts live in the excluded run dir (Issue #302): plan, test log and coverage data go to `.orbi/` (`.orbi/plan.md`, `.orbi/test.log`) — never at the worktree root, so the dirty-worktree gate never sees an orbi-produced artifact and a broken/renamed `.gitignore` weakens nothing (the Runner pins them in the local exclude).
- Coverage gate (Issue #234), tiered: the whole repository keeps line >= 95% and branch >= 95% (checked separately, never a merged single percentage); the Python lines and branches changed in the current PR keep 100% line/branch; the core state machines and critical failure paths (model_wait/idle recovery, Issue/label lifecycle, Git/PR/merge gate, config validation, deployment failure paths) keep 100% line/branch through their existing tests. Code below 95% is only allowed for non-core, clearly explainable legacy/defensive branches and must stay locatable in the coverage report — no unjustified `# pragma: no cover`. Full policy and the exact contract commands (the four `coverage run`/`report`/`coverage_gate.py`/`diff_coverage_gate.py` steps, run from the repository root): `docs/testing.mdx` (EN) / `docs/zh/testing.mdx` (ZH). The coverage data file lives in the excluded run dir — the worktree root never carries a coverage artifact.

## UI work

- Any UI task drives the real running app with Playwright: real interaction, an assert on the changed flow, console and network error checks, and a screenshot saved under the run artifacts.

## Fail fast

- Command errors fail fast: log the command, return code, stdout and stderr, then raise. Never swallow an error or add a fallback path.

## Observability (contract)

- Progress is automatic: no human status command, no polling, no supervision; `orbi status` is a debug attachment only.
- The journal is the record: a heartbeat at most every 30 seconds, every line prefixed `[run_id]`; a stalled session is recovered automatically, and a frozen `model_wait` past `PI_MODEL_WAIT_DEAD_SECONDS` (default 1800 s; the pre-#228 default was 600 s) is a hung model request — the Runner logs `model_wait_dead` (`upstream_alive` is evidence, never a veto) and kills the Pi session. It never fires while events keep arriving: a slow generation is not a hung request, and none of this is a business task timeout.
- GitHub: exactly one progress comment per run, PATCHed in place, with short milestone comments; it is a pure bypass — a `progress_publish_failed` never fails the delivery, never marks the Issue `ai-blocked`, and never skips `run_pi` / `delivery_step`. The `Orbi opened PR:` scene comment is NOT a bypass: the next tick's resume parses it, so a failure there fails the delivery fail-fast.
- Field reference and full mechanics: `docs/operations.mdx` (EN) / `docs/zh/operations.mdx` (ZH) (the README homepage keeps a one-sentence summary plus the link, Issue #241).

## Base freshness and deployment (contract)

- Every task worktree is created from the frozen `origin/<base_branch>` SHA (default `main`), never from the main worktree's current HEAD; the worktree, Pi session directory, and run artifacts carry the unique run id, while the branch name is stable by Issue.
- The agent stops at the committed delivery. The Runner re-fetches `origin/<base_branch>` under the shared base-sync lock and absorbs an advanced base with a plain `git merge` on the task branch, then pushes and opens the PR. A delivery is acceptable only when its HEAD contains the latest remote base. No auto conflict resolution, no force push, no merge or push of the protected branch.
- Execution-source freshness (Issue #525): before any slot or claim the Runner proves the code THIS process executes matches the ENGINE source channel — the host/deploy-only `engine_source_track` (Issue #535: absent/`main` fast-forwards `origin/main`; `branch:`/`release`/`tag:`/`sha:` follow or lock that ref; the delivery `base_branch` never influences it) — the import source's checkout `HEAD` for an editable install, the installed version vs the channel's release tag for a non-editable one (local git reads only). A stale or unverifiable source logs the structured `runner_source_stale` line (facts + fix) and fails the start; `allow_stale_runner: true` downgrades it to a warning. The self-check only protects versions that carry it — the outermost defense is the `ExecStartPre` preflight (the CLI probe self-heal, then `orbi sync-engine-source` applying the channel), executed by systemd before the Runner process exists.
- Engine source update channel (Issue #535): the deploy home checkout follows `engine_source_track` in the deploy home's `orbi.toml` (host-only — a repository's `.github/orbi.toml` can never carry it). The `ExecStartPre` sync (`orbi sync-engine-source`) applies the channel fail-closed (dirty checkout, missing tag/SHA, non-fast-forwardable branch or unverifiable state = structured line + no start) and logs `engine_source_synced engine_source_track=... resolved=... head=...`; while the channel is anything but the plain `main` track (`branch:`/`release`/`tag:`/`sha:`) the Runner skips its post-merge fast-forward of the deployment checkout. `install.sh` new installs default to `release`; Orbi's dogfood pins `main`.
- The repo templates `systemd/orbi@.service` and `systemd/orbi@.timer` are the single source of truth for the installed user units. `orbi install-units` is the idempotent install (copy, legacy migration, `daemon-reload`, enable the timer instances): it NEVER starts/stops/restarts the service — a running Runner is never killed or restarted by an install.
- A template change is a deployment change: it takes effect without a human step — the next timer trigger's `ExecStartPre` syncs the checkout, and the pre-start drift check self-heals the installed units with the same install (`unit_drift result=auto_synced` line per unit). A drift the self-heal cannot resolve is caught by the pre-start check: the `unit_drift` line, fail fast — no slot, no claim, no label change. `orbi doctor` is the read-only report.
- The service `ExecStart` is the installed `orbi` CLI (an editable `uv tool` install; ordinary source/template changes need no reinstall or upgrade). The runtime code lives in the `src/orbi/` package (Issue #168 src layout): the editable finder maps the WHOLE package directory, so a newly added package module needs no reinstall (the #158 stale-module-list root cause). A stale or non-editable CLI source is reported by `orbi doctor` (`cli_source: DRIFT`) and repaired by `orbi setup`. When the breakage is severe enough that the console script cannot even `import orbi` (the #248 scene: a src-layout migration left the installed editable finder stale), the service's FIRST `ExecStartPre` step self-heals OUTSIDE Python: it probes `orbi --version` and, on probe failure, runs the editable force-reinstall under the same `base-sync.lock` flock (the #158 in-Runner refresh is unreachable in that scene); the SECOND step (`orbi sync-engine-source`, Issue #535) needs that working CLI. The checkout root carries NO `orbi.py`: a flat file named like the package would shadow the installed package for every process with the checkout root on sys.path; the direct-execution compatibility entry is `python3 -m orbi.cli` (development/compatibility path, never the documented usage).
- Mechanics (timer instances, the pre-start preflight sequence, the unit-drift self-heal, the slots): `docs/operations.mdx` (EN) / `docs/zh/operations.mdx` (ZH); the CLI install refresh (`cli_install_failed`) is documented in `docs/getting-started.mdx`.

## Git transport (Issue #114, #580)

- The delivery checkout's git data operations (fetch, push — including pushing `.github/workflows/*.yml`) run over the transport configured by `git_transport` in orbi.toml: `ssh` (default; the pre-#580 contract — `git@github.com:owner/repo.git`, machine SSH key) or `https` (`https://github.com/<repo>.git`, credentials via the `gh` credential helper — the token-only sandbox path with no SSH private key). GitHub API operations (Issue, PR, label, comment, merge) always stay on the `gh` token; SSH is never used as API authentication.
- Pre-start check: the configured `origin` URL matches the configured transport for the first source repo and `git ls-remote <expected-url>` exits 0; a failure logs `transport_check_failed` and fails the start — no slot, no claim, no label change, no fallback (in ssh mode explicitly no HTTPS fallback), no silent skip (the probe-failure reason is `ssh_unreachable` in ssh mode, `transport_unreachable` in https mode — fix the gh credentials there).
- A remote on the opposite transport is never rewritten silently: only the human-run `orbi setup` migrates it with `git remote set-url origin <expected-url>` (HTTPS→SSH in ssh mode, SSH→HTTPS in https mode); every other path fails fast with the exact migration command. `orbi doctor` reports the transport read-only.
- Full explanation: `docs/operations.mdx` and `docs/setup.mdx` (EN/ZH).

## Always widen gh list queries

`gh api <list endpoint>`, `gh issue list` and `gh pr list` return only the first
page by default. **An empty result is not an error** — the query succeeds, exits
0, warns about nothing, and "not on this page" gets read as "does not exist".

- `gh api` takes `--paginate`
- `gh issue list` / `gh pr list` take `--limit 200 --state all`
- for versions use `gh release list --limit 10`

If you only want a sample, say so; do not conclude "does not exist" from it.

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
- **Claim layer — Milestone completeness gate** (`_pick_from_scan`): while the release's Milestone has any other open Issue (`open_issues > 1`), the release is not claimed at all; it is skipped with the structured `release_milestone_incomplete` line and remains `ai-ready`, a recoverable wait that never becomes `ai-blocked`. A Milestone query that cannot be evaluated fails safe and skips with `release_milestone_check_failed`. The ordinary scans skip `ai-release` Issues with `release_not_claimed`; only the release fallback scan may claim one (`allow_release=True`).
- **Gate layer — delivery wait** (`release_waiting_deliveries`): after the claim layer has allowed the release to be claimed, only same-Milestone Issues carrying `ai-in-progress`, `ai-pr-opened`, or `ai-fix-needed` hold the release; a bare `ai-ready` Issue does not. This bounded wait uses `RELEASE_DELIVERIES_WAIT_SECONDS`; exceeding it produces a delivery wait timeout and moves the release to `ai-blocked`. Therefore, a release queued behind other open tickets is not accumulating this timeout: it has not been claimed, `wait_started` is not written, and `release_waited_seconds` is `0.0`.
- **Scope layer — open evidence** (`open_milestone_evidence`): this does not wait. Still-open Issues in the Milestone are excluded from the released scope and recorded as `NOT released (still open in milestone <name>): <item>` evidence.
- Release state-machine contract: `docs/workflow.mdx` (EN) / `docs/zh/workflow.mdx` (ZH).

## Review, fix and merge (same PR)

- The review session is independent (a new Pi process, `prompts/prompt_review.md`, a new JSONL) and ALSO the fixer: it may modify code, run the full suite with the tiered coverage gate (Issue #234: whole repository line/branch >= 95% checked separately, changed Python code at 100%), commit, and push ONLY the task branch, then re-emit the `REVIEW_VERDICT` for the fixed head. A `pass` verdict means zero Blocker/Major findings AFTER the in-session fixes; a missing or malformed verdict fails fast and is never treated as a pass.
- The review prompt receives a fresh Issue body at the start of every round. Editing the Issue body takes effect from the next round; the current body is authoritative over prior findings, so a finding whose acceptance criterion was removed must not be re-raised.
- `ai-pr-opened` means awaiting review; `ai-fix-needed` marks a delivery whose head is not mergeable yet (a finding the session could not fix, a PR behind the latest base / with a merge conflict, or an AI-recoverable failure of the existing run/PR): the NEXT tick resumes the SAME run_id, branch, worktree and PR and runs the next independent review session, which absorbs the latest base in-session. Never a replacement PR, never a re-claim.
- The merge gate re-fetches the latest remote base and requires the PR head to contain it, the PR to be mergeable, and the remote head to still be the reviewed head; the merge lands exactly that head (`gh pr merge --match-head-commit`).
- The review loop is bounded (5 rounds); exhausting rounds with findings fails fast and marks the Issue `ai-blocked`.
- External contributor PRs (Issue #608): the triage workflow files CI-failure Issues that link the PR, marking an EXTERNAL one (head outside the stable `orbi/{repo}-issue-{N}` delivery naming) with the hidden `<!-- orbi:external-pr:<n> -->` marker. A requeued external triage ticket reaches the queue through the FIFTH, milestone-free claim scan (Issue #842 decision D1: the scan after p0/bug/plain and the release fallback, keyed on the body marker — an external contribution belongs to no version's milestone, and writing one would let an external PR block the release gate). Claiming such an Issue takes the external PR over for review FIRST (the same review loop, worktree checked out at the external head; no `run_pi`); a clean verdict is the engine's FINAL step — the review conclusion is posted on the Issue and the ticket stops at `ai-blocked`, NEVER auto-merged, NEVER given a milestone (Issue #842 decision D2: acceptance and version choice are the maintainer's); a human merge closes the triage Issue (the PR carries no `Fixes` keyword), and an external PR closed without a merge requeues the Issue (`requeue` label event) for an internal redo. External PRs are never silently redone while open; forks are out of scope. The release gate has NO open-PR check: open PRs are queue state, not a release premise.
- Chain, state semantics and label lifecycle: `docs/workflow.mdx` (EN/ZH); the recovery scene contract (trusted-maintainer comment, derived branch/worktree, PR body run marker): `docs/security.mdx` + `docs/workflow.mdx` (EN/ZH); the contributor-facing external-PR flow: `docs/contributing.mdx` (EN/ZH).

## Run correlation

- One task attempt generates one run_id (8 hex chars) and reuses it for every later step of the attempt; a retry generates a new one. No new id system: no trace_id, no log_id, no second UUID, no tracing backend.
- Every journal line of the attempt starts with `[run_id]`; every Issue/PR comment and the PR body carry the stable marker `<!-- orbi:run=<run_id> -->` plus the visible `run_id=` field; branch, worktree, Pi session dir and run artifacts carry it in their paths. A run-scoped event without a valid run_id fails fast.
- Explanation: `docs/workflow.mdx` (EN/ZH).

## Git

- Work on the task feature branch.
- Pi (the implementer) does not merge and does not push `main` or `master`, and never force-pushes any shared branch. It delivers through exactly one PR linked to the Issue; the Runner is the only merge actor.
- The PR description must contain `Fixes #<issue-number>` (it may be on the first line), pointing at the source Issue so GitHub closes the Issue natively when the PR merges into the default branch. The keyword works in the PR body and in commit messages, but not in the PR title. The runner rejects a PR whose body is missing it.

## Operating the environment (read before reaching for a tool)

These are not style preferences. Each one below cost a real debugging detour,
and the failure mode they share is that the wrong tool **does not error** — it
returns a plausible answer about the wrong thing.

- **Driving a browser: use the Chrome that is already signed in.** GitHub App
  installation, OAuth consent and GitHub's sudo-mode prompt are human
  authorisation steps with no API: `gh` returns 403 on `/user/installations`
  (needs an App-authorised token) and 401 on `/repos/{o}/{r}/installation`
  (needs the App private key, which is deliberately outside the sandbox).
  Launching a fresh Playwright/Chromium gives a profile with no session, so it
  lands on the sign-in wall and the task looks blocked when it is not. Drive the
  user's existing browser instead (in Claude Code: the `claude-in-chrome`
  tools). Playwright is still the right tool for rendering checks and
  screenshots of pages that need no session.
- **GitHub's sudo mode: choose "Use GitHub Mobile".** It shows two digits the
  user taps in the app — no password, no passkey. The digits expire in about a
  minute, so surface them immediately and only while the user is present.
- **`repo_dir` exists at the same path on more than one machine.** The
  delivery checkout path on the runner host is also a valid path on a
  maintainer's workstation. Running `git worktree list` / `git branch` /
  `git cat-file` locally then answers about the wrong checkout **and exits 0**,
  which reads as "already cleaned up". Resolve `repo_dir` from the runner's
  own config and run git there (over ssh if that host is remote) before
  concluding anything about branches, worktrees or objects.
- **Never extend a short SHA into a full one.** A 40-char SHA reconstructed
  from a 7- or 8-char prefix is a fabricated value that looks legitimate.
  `gh pr merge --match-head-commit` refuses it, and that refusal is the tool
  being right. Re-read the full value
  (`gh pr view N --json headRefOid --jq .headRefOid`) instead of padding.
- **A gate that only asks "did anything fail?" passes on an empty set.**
  When reviewing or writing an admission check, test the empty input
  explicitly: "nothing reported a failure" and "nothing has run yet" must not
  take the same branch. Issue #1243 is the worked example — an empty
  `statusCheckRollup` satisfied every merge precondition.

## Scope

- No database, queue, daemon loop, risk engine, or fallback. GitHub Issues and labels are the only state store.
- No business task timeout. systemd only schedules the tick and owns the run lifecycle.
