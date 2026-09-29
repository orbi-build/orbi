#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
export GOTOOLCHAIN=go1.26.6
cp -R "$HERE/hidden/." "$DIR/"

rc=0
if go test -count=1 -run '^TestCommand_BareDash$' .; then echo "PASS: target test (^TestCommand_BareDash$)"; else echo "FAIL: target test (^TestCommand_BareDash$)"; rc=1; fi
if go test -count=1 .; then echo "PASS: package tests (.)"; else echo "FAIL: package tests (.)"; rc=1; fi
if go vet .; then echo "PASS: go vet (.)"; else echo "FAIL: go vet (.)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
