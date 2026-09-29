#!/usr/bin/env bash
# usage: score.sh <checkout-dir> <base-sha>
set -u
cd "$1" || exit 2
BASE=$2
export PATH=/tmp/claude-1000/-home-xqianliu-Projects-orbi/7f781b44-47a6-4929-88f4-74dd56ea7b9e/scratchpad/denobin/node_modules/.bin:$PATH NO_COLOR=1
fail=0
if git diff --name-only --diff-filter=A "$BASE...HEAD" -- changes.d | grep -q .; then echo "PASS rule: changelog fragment added"; else echo "FAIL rule: no changes.d fragment"; fail=1; fi
if ~/orbi-bench/tools/bin/sacho check > /tmp/fesacho.log 2>&1; then echo "PASS rule: sacho check (CHANGES.md in sync with fragments)"; else echo "FAIL rule: sacho check: $(grep -vE '^skipped' /tmp/fesacho.log | head -2 | tr '\n' ' ' | cut -c1-120)"; fail=1; fi
[ -f packages/vocab/src/vocab.ts ] || deno task -f @fedify/vocab compile >/dev/null 2>&1
deno fmt --check packages/testing/src >/dev/null 2>&1 && echo "PASS fmt" || { echo "FAIL fmt"; fail=1; }
deno lint packages/testing/src >/dev/null 2>&1 && echo "PASS lint" || { echo "FAIL lint"; fail=1; }
cp ~/orbi-bench/oracle/fedify/zz_oracle.test.ts packages/testing/src/zz_oracle.test.ts
(cd packages/testing && deno test --allow-all --unstable-kv --no-check src/zz_oracle.test.ts) > /tmp/feo.log 2>&1 && echo "PASS oracle" || { echo "FAIL oracle: $(grep -E '^oracle.*FAILED|=> ' /tmp/feo.log | head -4 | tr '\n' ' ')"; fail=1; }
rm -f packages/testing/src/zz_oracle.test.ts
(cd packages/testing && deno test --check --allow-all --unstable-kv) > /tmp/fes.log 2>&1 && echo "PASS suite" || { echo "FAIL suite: $(grep -E '^(FAILED|error)' /tmp/fes.log | head -2 | tr '\n' ' ')"; fail=1; }
exit $fail
