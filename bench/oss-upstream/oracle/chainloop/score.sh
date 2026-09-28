#!/usr/bin/env bash
set -u
cd "$1" || exit 2
export GOTOOLCHAIN=go1.26.6
fail=0
gofmt -l internal pkg | grep -q . && { echo "FAIL gofmt"; fail=1; } || echo "PASS gofmt"
go vet ./internal/redaction/ ./pkg/attestation/crafter/... > /tmp/clv.log 2>&1 && echo "PASS vet" || { echo "FAIL vet"; fail=1; }
cp ~/orbi-bench/oracle/chainloop/zz_oracle_test.go internal/redaction/zz_oracle_test.go
go test -count=1 -run '^TestStrict' ./internal/redaction/ > /tmp/clstrict.log 2>&1 && echo "INFO strict: newline kept" || echo "INFO strict: newline dropped (as base)"
go test -count=1 -run '^TestOracle' ./internal/redaction/ > /tmp/clo.log 2>&1 && echo "PASS oracle" || { echo "FAIL oracle: $(grep -oE -- '--- FAIL: TestOracle[A-Za-z]+' /tmp/clo.log | sed 's/--- FAIL: //' | tr '\n' ' ')"; fail=1; }
rm -f internal/redaction/zz_oracle_test.go
go test -count=1 ./internal/redaction/... ./pkg/attestation/crafter/... > /tmp/cls.log 2>&1 && echo "PASS suite" || { echo "FAIL suite: $(grep -E '^(--- FAIL|FAIL)' /tmp/cls.log | head -3 | tr '\n' ' ')"; fail=1; }
exit $fail
