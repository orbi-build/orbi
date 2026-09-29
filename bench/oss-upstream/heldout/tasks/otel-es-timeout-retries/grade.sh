#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
export GOTOOLCHAIN=go1.26.6
cp -R "$HERE/hidden/." "$DIR/"
cd exporter/elasticsearchexporter || { echo "FAIL: module dir missing"; exit 1; }
go mod download >/dev/null 2>&1 || true

rc=0
if go test -count=1 -timeout 60s -run 'TestExporterTimeout' .; then echo "PASS: target tests (TestExporterTimeout_*)"; else echo "FAIL: target tests (TestExporterTimeout_*)"; rc=1; fi
if go test -count=1 -timeout 170s -skip 'TestExporterTimeout' ./...; then echo "PASS: module tests (exporter/elasticsearchexporter/...)"; else echo "FAIL: module tests (exporter/elasticsearchexporter/...)"; rc=1; fi
if go vet ./...; then echo "PASS: go vet"; else echo "FAIL: go vet"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
