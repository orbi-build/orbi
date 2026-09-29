#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
cp -R "$HERE/hidden/." "$DIR/"

if [ ! -x node_modules/.bin/mocha ] || [ ! -x node_modules/.bin/tsc ]; then
  if ! npm ci --ignore-scripts --no-audit --no-fund >/tmp/grade-recast-install.log 2>&1; then
    tail -20 /tmp/grade-recast-install.log; echo "FAIL: npm ci"; exit 1
  fi
fi
# tsc compiles lib/ and test/*.ts -> *.js; tests run from the compiled output
if node_modules/.bin/tsc; then echo "PASS: build (tsc)"; else echo "FAIL: build (tsc)"; exit 1; fi

rc=0
cd test || exit 1
# test/run.sh is not used: it clones babel/graphql-tools fixtures from the network
if ../node_modules/.bin/mocha --reporter dot printer.js; then echo "PASS: target tests (test/printer.js)"; else echo "FAIL: target tests (test/printer.js)"; rc=1; fi
FILES=$(ls *.js | grep -v '^run\.js$')
if ../node_modules/.bin/mocha --reporter dot $FILES; then echo "PASS: all offline test files (test/*.js)"; else echo "FAIL: all offline test files (test/*.js)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
