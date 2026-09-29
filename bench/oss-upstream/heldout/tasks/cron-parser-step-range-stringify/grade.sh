#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
cp -R "$HERE/hidden/." "$DIR/"

if [ ! -x node_modules/.bin/jest ]; then
  if ! npm ci --ignore-scripts --no-audit --no-fund >/tmp/grade-cron-install.log 2>&1; then
    tail -20 /tmp/grade-cron-install.log; echo "FAIL: npm ci"; exit 1
  fi
fi
export TZ=UTC

rc=0
if node_modules/.bin/jest --ci tests/CronExpression.test.ts tests/CronExpressionParser.test.ts; then echo "PASS: target tests (CronExpression + CronExpressionParser)"; else echo "FAIL: target tests (CronExpression + CronExpressionParser)"; rc=1; fi
if node_modules/.bin/jest --ci; then echo "PASS: full jest suite (tests/)"; else echo "FAIL: full jest suite (tests/)"; rc=1; fi
if node_modules/.bin/tsc -p tsconfig.json --noEmit; then echo "PASS: typecheck (tsc --noEmit)"; else echo "FAIL: typecheck (tsc --noEmit)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
