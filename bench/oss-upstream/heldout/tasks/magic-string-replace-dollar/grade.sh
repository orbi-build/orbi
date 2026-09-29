#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
cp -R "$HERE/hidden/." "$DIR/"

if command -v pnpm >/dev/null 2>&1; then PNPM=(pnpm); else PNPM=(npx -y pnpm@12.3.4); fi

# lifecycle scripts skipped on purpose (prepare = build, done explicitly below)
if ! "${PNPM[@]}" install --frozen-lockfile --ignore-scripts >/tmp/grade-ms-install.log 2>&1; then
  tail -20 /tmp/grade-ms-install.log; echo "FAIL: pnpm install"; exit 1
fi
# Bundle/SourceMap/index tests import the built package, so build first
if "${PNPM[@]}" exec tsdown >/tmp/grade-ms-build.log 2>&1; then echo "PASS: build (tsdown)"; else tail -20 /tmp/grade-ms-build.log; echo "FAIL: build (tsdown)"; exit 1; fi

rc=0
if "${PNPM[@]}" exec vitest run test/MagicString.test.ts -t 'replace'; then echo "PASS: target tests (test/MagicString.test.ts -t replace)"; else echo "FAIL: target tests (test/MagicString.test.ts -t replace)"; rc=1; fi
if "${PNPM[@]}" exec vitest run; then echo "PASS: full vitest suite (test/)"; else echo "FAIL: full vitest suite (test/)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
