# Triage: external Issues #1358–#1361 (filed 2026-09-25) and the fork PR #1419

All four Issues were filed by one external user (@TianQingX, `author_association: NONE`) against v0.5.46.
Each claim was checked against `main` at `ad423e7`. None of them carries `ai-ready`; nothing below is labelled or
filed yet. Every recommendation waits on the maintainer.

## Summary

| Issue | Premise true in code? | Kind | Fix size | Recommendation |
|---|---|---|---|---|
| #1361 doctor says "requires admin" on a Free-plan 403 | Yes (`github.py:121-129`) | **Bug**: the message contradicts reality | S | Accept, `ai-ready` + `bug` |
| #1360 Pi agent dir hard-coded to `~/.pi/agent` | Yes (`pi_session.py:351`) | Gap / feature (new config key) | S | Accept one key, `pi_agent_dir`; drop `review_pi_agent_dir` |
| #1359 `ai-human-review` accepted from any actor | Yes (`runner.py:6202`) | Design choice, not a bug | M if built | Decline for now and reply with the reasoning |
| #1358 a second deployment on the same repo restarts a live delivery | Yes (`claim.py` `held` comes from the local `slot_dir`) | Unsupported topology | Docs: S; real support: L | Docs-only: one deployment per task pool |

Side finding: **#1420** (`bug` + `p0` + `ai-ready`, filed by the CI-failure workflow) is noise. It is the CI failure of
the fork PR #1419, which its author closed at 12:41:52Z, one second before the triage ran. So
`commits/{sha}/pulls` resolved no PR ("no PR resolved"), and the failure was filed like one on an internal branch.
It has no milestone, so the active-milestone scans skip it (`ready_outside_milestone`) and it will not burn a run.
Close it as not planned. No new ticket is proposed for this; it has happened once.

---

## #1361: plan-limit 403 on the rulesets API reported as a permission problem

**Verified.** `merge_gate_preflight` (`src/orbi/github.py:121-129`) maps every non-404 `CalledProcessError` from
`rules/branches/<b>` to `UNKNOWN cannot read rulesets; requires repository administration permission`. The raw
`ERROR command_failed` line comes from `run_gh_read_command`, which logs at ERROR by default. GitHub's plan-limit
body (`"Upgrade to GitHub Pro or make this repository public to enable this feature."`, HTTP 403) is a different
condition, and the repair the message suggests (grant admin) cannot fix it. Under AGENTS.md this counts as a bug:
the output contradicts the facts.

**Design (smallest).**
1. Add a module-level predicate next to `_not_found`: `_plan_limited(exc)` matches the documented GitHub text
   `Upgrade to GitHub Pro` in stderr/stdout. Match on the message, because the status code alone (403) is
   exactly what a real permission failure also returns.
2. Call the rulesets endpoint with `failure_log_level=logging.DEBUG`. `run_gh_read_command` already supports
   this (`github.py:236`, and `:428` uses it the same way), so an expected probe failure leaves no `ERROR`
   line. Real failures still surface through the returned `UNKNOWN` line.
3. On `_plan_limited(exc)`, emit `merge_gate_rulesets_unavailable reason=plan` and set `rules = []`, then continue
   with the classic-protection result. On a Free private repo, `branches/<b>.protected` is `false`, so the
   result is the plain pass.
4. Keep every other non-404 path returning today's `UNKNOWN ... requires repository administration permission`.

**Hard constraints the fork PR #1419 tripped on** (its CI failed on these, not on behaviour):
- `github.py` is at **1162 / 1162** on the size ratchet (`tools/size_ratchet.py`). Any net line added fails CI.
  The delivery must either stay net-zero in `github.py` or, per the ratchet's own instruction, move
  `merge_gate_preflight` + `_merge_gate_api` into a sibling module (for example `src/orbi/merge_gate.py`) and
  lower the ceiling.
- The patch ratchet (Issue #789) forbids new `monkeypatch.setattr` on `github` in tests (#1419 went 21 → 23).
  Drive the 403 through `tests/fakes/` (a fake `gh` returning the plan-limit body), not by patching.
- The new journal event must be registered in `JOURNAL_EVENTS` and in `docs/operations.mdx` / `docs/zh/operations.mdx`.

**Acceptance** (as in the Issue): Free-plan private repo + unprotected base → no `UNKNOWN`, no `ERROR`; a
permission 403 without the plan text still returns the admin message; the existing preflight tests stay green.

**About #1419:** the approach matches this design. Credit @shobhitagnihotri69 in the delivery PR. Their PR was
closed by its author, has no `Fixes #1361`, and its CI was red, so let the Orbi delivery take #1361.

---

## #1360: every deployment shares `~/.pi/agent/auth.json`

**Verified.** `prepare_pi_agent_dir` (`src/orbi/pi_session.py:351`) always reads
`Path(os.path.expanduser("~")) / ".pi" / "agent"`: the `auth.json` symlink target and the `models.json` /
`settings.json` merge base. When no provider file is configured, the function returns `None` and Pi inherits the
Runner's environment, so `PI_CODING_AGENT_DIR` from the service env would already work there. But the systemd
units are shared templates (never hand-written), so the only per-deployment carrier is `orbi.toml`.

**Kind.** This is a real gap for the documented *One machine, multiple deployments* (#747) setup, but no document
promises per-deployment Pi auth. So it is a feature request and needs a yes from the maintainer.

**Design (smallest).**
1. Add one host-only key, `pi_agent_dir` (a path, `~` expanded). The default is the current `~/.pi/agent`, so the
   unset case is byte-identical.
2. `prepare_pi_agent_dir` reads it instead of the hard-coded path.
3. When there is no provider file (the `None` path), also export `PI_CODING_AGENT_DIR=<pi_agent_dir>` when the
   key is set, so the key means the same thing on both paths.
4. Leave `review_pi_agent_dir` out (KISS). Two different OAuth logins for implement and review is a second use
   case nobody has hit yet. With one key, two deployments can already hold two logins.
5. Config validation: a set value must be an existing directory. Otherwise fail the config load fast, like the
   other host keys. Add the key to `docs/configuration.mdx` (EN/ZH).

**Acceptance:** with the key set, the per-run `auth.json` symlink targets `<dir>/auth.json` and the merge reads
`<dir>`; with it unset, the existing `prepare_pi_agent_dir` tests pass unchanged; a bad path fails config load
with the key name.

---

## #1359: `ai-human-review` is accepted from any actor

**Verified premise.** The gate checks only that the label is present (`runner.py:6202`); no actor and no head
binding are read.

**Why this is not a bug.** The gate's documented purpose (`human_review.py` docstring, `docs/configuration.mdx`
`human_review_gate`) is for a person to confirm the **column-2 checklist**: business intent, UI, external
environment. It is not a code-approval signature. Two points in the Issue follow from that design:
- *"Any automation holding the same token can open it."* Any process holding the maintainer's token can also
  `gh pr merge` the PR directly, or edit the host `orbi.toml`. An actor check in the Runner therefore adds no
  protection against a same-token script. The real boundary is who holds the credential, and that is outside Orbi.
- *"A label added before a later push still approves the new head."* The review session is also the fixer by
  contract (AGENTS.md, *Review, fix and merge*). It may push after the human confirmed intent, and binding the
  label to one head would re-block every delivery that needs a review fix.

The one scenario where an allow-list helps is approval from a *second* GitHub account. That is a new capability
(`human_review_approvers`, reading the timeline events or PR reviews). It is size M: timeline pagination, the head
binding, and new wait reasons. No user has needed it beyond this report.

**Recommendation:** no ticket now. Reply on #1359 with the reasoning above, and keep it open as an enhancement
until a user with a separate approver account needs it.

---

## #1358: a second deployment on the same repository restarts a live delivery

**Verified premise.** The in-flight restart scan is not milestone-scoped (deliberately: resume states are never
gated). The `held` set that protects a live delivery comes from `<repo_dir>/.orbi/slots/`
(`slot_held_deliveries(slot_dir, ...)`), and so does the #1319 claim lock. A deployment with a different
`repo_dir` cannot see them, so it takes the other deployment's live Issue for a lost in-flight run.

**Why this is not a bug.** The reporter sharded one repository across six deployments with distinct
`active_milestone`s. Nothing in the product supports that: `source_repos` is "one task pool", the whole
concurrency model is local slots, and the docs already state the principle for the other two runner types
("Managed Cloud and a self-hosted runner must not both serve the same task pool", "Docker ... never both for the
same task pool", `docs/installation.mdx:17-19`). The #747 section only says that several *independent* deployments
may share a machine; it never says they may share a repository. That silence is the real defect.

**Why not the Issue's option 1** ("skip when the newest run's last activity is recent and unknown locally"):
- It is timing-based, ad hoc logic (AGENTS.md: *no ad hoc special cases*).
- It is indistinguishable from the #1176 case it must preserve: a deployment that really lost its worktree also
  sees a recent run it has no local slot for.
- Doing it properly means a cross-host lock on GitHub state, which is the distributed-scheduler territory the
  MVP scope excludes (no DAG, no multi-worker scheduling).

**Design (docs only, allowed as a direct edit):** add one rule to `docs/getting-started.mdx` §*One machine,
multiple deployments* (and `docs/zh/`) plus the `docs/installation.mdx` bullet list: *"Each deployment must serve a
different `source_repos` task pool. Two deployments on one repository, even with different `active_milestone`s, will
restart each other's live deliveries: one task pool, one runner."* To run milestones in parallel on one repository,
raise `max_concurrency` in one deployment instead.

Optional detection (only if the maintainer wants it; size S–M, needs a ticket): `orbi doctor` reads the other
deployments' configs on the machine (it already reads unit files and their `ORBI_CONFIG` for `unmanaged_units`)
and reports `FAILED shared_task_pool` when two configs name the same `source_repos`. This check is static and
local, with no timing heuristic. It cannot catch deployments on different machines. It is not proposed by default
(AGENTS.md: no preventive gates for incidents that might recur).

---

## Proposed next steps (each waits on a maintainer yes)

1. #1361 → label `ai-ready` + `bug`; add the size/patch ratchet constraints above to the body.
2. #1360 → narrow the body to the single `pi_agent_dir` key, then `ai-ready`.
3. #1358 → direct docs edit (one-task-pool rule), reply on the Issue, close as not planned (unsupported topology).
4. #1359 → reply with the reasoning, relabel `enhancement`, leave open.
5. #1420 → close as not planned (the fork PR's author closed it before the triage ran).

## Milestones assigned (2026-09-26)

Rule from the maintainer: bugs go into a new 0.5.x milestone, new features into v0.6.0.

- #1361 (bug) → **v0.5.51** (new milestone; v0.5.50 is already releasing).
- #1358, #1359, #1360 (feature / unsupported topology) → **v0.6.0**.

No `ai-ready` label was added; the maintainer labels each ticket when its body is final.
