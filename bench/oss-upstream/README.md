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
