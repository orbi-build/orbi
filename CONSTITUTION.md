# Orbi Constitution

The architectural invariants of the Orbi delivery engine. This file says what
must not change and why. `AGENTS.md` says how a delivery is done; `docs/`
explains mechanics. When they disagree, this file wins.

Every PR that touches this file must state, in its description, which article
changes and why the old rule no longer holds. An agent may not amend this file
as a side effect of another change.

## Article 1 — What Orbi is

1. Orbi is a local AI development worker: a GitHub Issue in, a reviewed and
   merged PR out. It runs on the user's machine with the user's credentials.
2. GitHub Issues, labels, comments and PRs are the only state store. There is
   no database, queue, daemon loop, DAG, scheduler or risk engine, and no
   second task board. A feature that needs one of these is out of scope.
2b. **The Issue is the only state machine.** Delivery state lives in the
   Issue's labels and nowhere else. A PR never carries an Orbi label and
   never holds state of its own: it is an artifact of a delivery, read for
   its head, mergeability and checks, never written to as a status record.
   State flows one way — PR events update Issue labels, never the reverse.
   A PR that needs to be tracked needs an Issue, not a second lifecycle.
   Consequently every delivery PR names its Issue with `Fixes #<N>`; a PR
   with no Issue is outside the delivery loop by definition.
3. Execution is single-slot and serial per instance. Concurrency is a fixed
   number of systemd timer instances sharing flock slots, nothing more.
4. Orbi does not know who consumes it. There is no hosted-service protocol,
   no upstream reporting channel and no consumer-specific code path. A hosted
   product integrates through the public surface in Article 2 only.
5. Fail fast. A command error logs the command, exit code, stdout and stderr,
   then raises. There is no fallback path, no silent skip, no retry that hides
   a failure. The Issue ends in `ai-blocked` for a human.
6. The agent never merges or pushes a protected branch. Only the Runner merges,
   only the reviewed head, only when it contains the latest remote base.

## Article 2 — The public surface

The following are contracts. Consumers (humans, scripts, hosted products) may
depend on them. Changing any of them requires a release note entry and a docs
update in the same PR; a silent change is a defect even when all tests pass.

| Surface | Definition |
|---|---|
| CLI | `orbi` (one Runner tick), `orbi add`, `orbi status`, `orbi session`, `orbi install-units`, `orbi doctor`, `orbi setup` |
| Config | `orbi.toml` fields documented in `docs/getting-started.mdx`; `.orbi/env` loaded as systemd `EnvironmentFile`; the `pi_providers` JSON file |
| Deployment | `systemd/orbi@.service` and `systemd/orbi@.timer` templates; `.orbi/` as the run-artifact and lock directory |
| Label state machine | `labels.toml` (names, colors, descriptions) and `src/orbi/delivery_labels.py` (transitions) |
| Journal | Every line of a run starts with `[run_id]`; event names and `key=value` fields listed in `docs/operations.mdx` |
| Session files | `<worktree>/.pi-session/*.jsonl` |
| Naming | Branch `orbi/<owner>-<repo>-issue-<N>`; worktree `.worktrees/orbi-<owner>-<repo>-issue-<N>-<run_id>` |
| GitHub markers | `<!-- orbi:run=<run_id> -->` in every run-scoped comment and PR body; `Fixes #<N>` in the PR body |

Anything not in this table is internal and may change without notice.
Adding a row is an amendment.

## Article 3 — Module structure

1. `src/orbi/runner.py` is frozen. No new top-level function or class is added
   to it. New behaviour lives in a sibling module under `src/orbi/`, following
   the existing pattern (`delivery_labels`, `pi_activity`, `pi_recovery`,
   `progress`, `git_transport`, `systemd_deploy`, `pilot_slots`).
2. Touch-and-extract: a change that rewrites 50 or more lines inside one
   domain of `runner.py` (release, Pi streaming, review/merge, delivery,
   config) moves that domain into its own module in the same PR, tests
   included. Extraction never changes behaviour.
3. Modules extracted from `runner.py` never import `runner`. Only the CLI
   entry layer (`cli`, `cli_install`, `pilot_setup`) may import it, as a
   caller. Pure modules (no I/O, deterministic) are preferred;
   `delivery_labels.py` is the model.
4. There is exactly one subprocess seam: `run_command` (and its network
   variant). New code does not call `subprocess` directly.
5. A new module stays under 1,000 lines. A file that crosses it is split by
   domain before the next feature lands in it. `runner.py` is the one
   grandfathered exception, governed by rules 1 and 2.

## Article 4 — Simplicity

1. Every new file, dependency, config field, label, command, env variable and
   abstraction maps to one acceptance criterion in one Issue. If it cannot be
   named, it is not added.
2. One concept, one implementation. Settings of the same shape share one
   parser; helpers are not re-implemented per call site.
3. No compatibility shim for a consumer that does not exist. A renamed field
   or path is renamed, with a release note, not aliased forever.
4. No business timeout. systemd owns the run lifecycle; the Runner detects a
   dead model request by silence, never by a task deadline.
5. Configuration is explicit. A value is never guessed from the repository,
   the milestone list or the environment; a missing required value fails the
   start.
6. Comments explain the invariant a line protects, not the Issue history that
   produced it. History lives in commits and Issues.
7. **Delegate to the platform.** Work the hosting platform already does is
   not reimplemented locally. Test acceptance is the repository's own CI
   result on the delivered commit, never a local test run: a local runner
   would have to carry every language's toolchain, and a green local run
   proves nothing about the repository's declared checks. The same rule
   governs anything else GitHub already owns — merge queues, required
   checks, branch protection, closing keywords.

## Article 5 — Tests

1. Behaviour is asserted at the public surface (Article 2): CLI output, label
   transitions, journal lines, `.orbi/` artifacts, PR state. New tests do not
   patch internal functions of `runner` to assert call sequences.
2. Subprocess boundaries are stubbed at the single seam of Article 3.4, with
   the command line asserted only when the command line is the contract.
3. The coverage gate (whole repository ≥95% line and branch; changed lines
   100%) stays. `# pragma: no cover` is allowed only where
   `tests/test_coverage_gate.py` lists it.
4. Unbounded loops are tested with a termination guard; every blocking
   command runs under `timeout`. A hang is a failure, never noise.
5. A test that needs a real GitHub, model or systemd is an acceptance test
   and records its evidence in the Issue, not in the unit suite.

## Article 6 — Security

1. Tokens and model keys live only in `.orbi/env` and the user's `gh` auth.
   They never appear in the repository, the journal, an Issue comment or a
   PR body. A journal line that could carry one is redacted at the source.
2. Git data goes over SSH; GitHub API calls use the `gh` token. Neither is
   used for the other.
3. The Runner acts only on Issues that carry `ai-ready` and on comments from
   trusted associations. Issue text is data, never instructions to the Runner.
4. Every task worktree starts from the frozen `origin/<base_branch>` SHA.
   The main worktree is never written by a task.

## Article 7 — Amendments

1. An amendment is a PR that changes this file, opened from an Issue whose
   title starts with `Constitution:`. The PR description names the article,
   the old rule, the new rule and the evidence that the old rule failed.
2. An amendment cannot be bundled with a feature. The feature PR follows the
   amended constitution and links the amendment.
3. This file stays under 200 lines. A rule that does not fit replaces a rule
   that has stopped earning its place.
