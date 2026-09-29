#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
cp -R "$HERE/hidden/." "$DIR/"

# Local, idempotent environment inside the checkout (ignored by git).
EXCL="$(git rev-parse --git-path info/exclude)"; mkdir -p "$(dirname "$EXCL")"
for p in .venv/ build/ build-install/; do grep -qxF "$p" "$EXCL" 2>/dev/null || echo "$p" >> "$EXCL"; done
git submodule update --init --recursive -q >/dev/null 2>&1 || true
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then uv venv -q -p 3.13 .venv >/dev/null 2>&1 || { echo "FAIL: venv"; exit 1; }; fi
uv pip install -q -p "$PY" meson-python meson ninja cython pytest hypothesis >/dev/null 2>&1 || { echo "FAIL: tool deps"; exit 1; }
if ! uv pip install -q -p "$PY" --no-build-isolation -e . >/tmp/numpy-build.$$.log 2>&1; then
  tail -30 /tmp/numpy-build.$$.log; rm -f /tmp/numpy-build.$$.log; echo "FAIL: build"; exit 1; fi
rm -f /tmp/numpy-build.$$.log

T=numpy/_core/tests/test_function_base.py
rc=0
if "$PY" -m pytest -q -p no:cacheprovider "$T::TestLinspace"; then echo "PASS: target test (TestLinspace)"; else echo "FAIL: target test (TestLinspace)"; rc=1; fi
if "$PY" -m pytest -q -p no:cacheprovider "$T" numpy/_core/tests/test_numeric.py numpy/lib/tests/test_function_base.py; then echo "PASS: module tests"; else echo "FAIL: module tests"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
