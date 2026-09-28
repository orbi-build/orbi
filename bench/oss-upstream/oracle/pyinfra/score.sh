#!/usr/bin/env bash
# usage: score.sh <checkout-dir> ; prints PASS/FAIL lines, exit 0 only if all pass
set -u
cd "$1" || exit 2
uv sync -q >/dev/null 2>&1
fail=0
{ uv run ruff check -q && uv run ruff format --check -q && uv run mypy; } > /tmp/pyl.log 2>&1 && echo "PASS lint" || { echo "FAIL lint: $(tail -2 /tmp/pyl.log | tr '\n' ' ')"; fail=1; }
cp ~/orbi-bench/oracle/pyinfra/zz_oracle_test.py tests/test_cli/zz_oracle_test.py
uv run pytest -q -p no:cacheprovider tests/test_cli/zz_oracle_test.py > /tmp/pyo.log 2>&1 && echo "PASS oracle" || { echo "FAIL oracle: $(grep -E '^FAILED' /tmp/pyo.log | sed 's/.*:://' | tr '\n' ' ')"; fail=1; }
uv run pytest -q -p no:cacheprovider --disable-warnings -m 'not end_to_end' --ignore=tests/test_cli/zz_oracle_test.py > /tmp/pyf.log 2>&1 && echo "PASS suite" || { echo "FAIL suite: $(tail -1 /tmp/pyf.log)"; fail=1; }
rm -f tests/test_cli/zz_oracle_test.py
exit $fail
