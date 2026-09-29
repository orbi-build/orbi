#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
export GOTOOLCHAIN=go1.26.6
cp -R "$HERE/hidden/." "$DIR/"

rc=0
if go test -count=1 -run 'TestCliRun/match_function_with_empty_matches' ./cli; then echo "PASS: target test (TestCliRun/match_function_with_empty_matches)"; else echo "FAIL: target test (TestCliRun/match_function_with_empty_matches)"; rc=1; fi
if go test -count=1 . ./cli; then echo "PASS: package tests (. ./cli)"; else echo "FAIL: package tests (. ./cli)"; rc=1; fi
if go vet . ./cli; then echo "PASS: go vet (. ./cli)"; else echo "FAIL: go vet (. ./cli)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
