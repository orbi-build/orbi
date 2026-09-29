# OSS upstream benchmark

Runs the real Orbi runner end to end (claim, implement, PR, review, merge) on
private snapshot repositories of three upstream fixes Orbi once delivered with
defects, and scores the merged result with hidden oracles.

| Repo | Base | Original defect |
|---|---|---|
| pyinfra 1438 (CJK table width) | `0d34e0e8` | width rule made emoji modifiers and NFD marks worse than `len()` |
| chainloop 3481 (JWT redaction) | `55c73e1c` | trim leaked the tail of a password ending in a literal `\n`; short secrets over-replaced |
| fedify 1098 (mock setters) | `2152dfc6` | process only: first contribution must be claimed, bug fixes target a maintenance branch |

- `mkinst.sh <repo> <inst> <variant-dir>` creates `xqliu/obench-<inst>` (private, Actions off), the Issue, and a runner toml. A variant dir holds `prompt.md`, `prompt_review.md`, `engine.toml` (skills and `pi_*` lines), optional `pathprefix` (e.g. the Claude Code pi shim, tool bins) and `env`.
- `run2.sh <inst>` ticks `orbi` until the Issue is `ai-merged` or `ai-blocked`.
- `queue.sh <file> <K>` launches queued instances with a concurrency cap, a free-memory floor and a GitHub GraphQL budget floor (the budget is shared with every runner on the same account).
- `score.sh <inst>` clones the merged result and runs `oracle/<repo>/score.sh` (code: lint, hidden oracle tests, suite, repo rules such as `sacho check`) and `oracle/upstream_check.sh` (commit format, trailers, fork-local references). Every oracle was calibrated to fail on the base, fail on the original Orbi output where it was defective, and pass on a hand-verified fix.
- `agg.sh` prints pass/total per variant and repo; `export.py` writes `results.json`.

Paths assume `~/orbi-bench` as the working root.

## Held-out tasks (added 2026-09-29)

`heldout/tasks/<name>/` holds issues the harness was never tuned on. Each is a real
bug fix merged upstream after 2026-08-10; the hidden grader is the maintainer's own
test from the fix.

- `meta.env`: `REPO`, `BASE` (commit before the fix), `FIX` (commit after),
  and for regression-prone tasks `FIX_A` (the first upstream fix, which
  introduced a regression that `FIX` repaired).
- `issue.md` / `title`: the ticket Orbi sees. Rewritten with no upstream links,
  numbers or hints at the fix.
- `grade.sh <checkout>`: overlays `hidden/`, installs deps inside the checkout,
  runs the target tests plus the affected module's suite. Exit 0 = pass.
- `hidden.list`: the test files taken from `FIX`. We don't vendor upstream
  tests; `heldout/fetch_hidden.sh <tasks-dir> <cache-dir>` fetches them.
- `calib.txt`: evidence that the grader fails on `BASE` (and on `FIX_A` for
  regression-prone tasks) and passes on `FIX`.

Pipeline: `mkinst2.sh <task> <instance> <variant-dir>` → `run2.sh` →
`score2.sh` → `export_ho.py` / `stats.py` → `charts.py`. Scripts assume the
bench lives at `~/orbi-bench` (tasks under `~/orbi-bench/tasks`).

Isolation: `guard/gh` and `guard/git` go first on each held-out run's `PATH`
and refuse every repository except the run's own `xqliu/obench-*`, so an agent
can't look up the upstream fix. Snapshots are committed with `git add -A -f`
(ignored-but-tracked files) and include submodule contents.

Resources: run one instance at a time on a shared machine. `memguard.sh` kills
benchmark processes when available memory or free swap runs low, and `run2.sh`
points `TMPDIR` at the instance directory instead of a RAM-backed `/tmp`.
