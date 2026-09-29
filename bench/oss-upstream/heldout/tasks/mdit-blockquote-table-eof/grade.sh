#!/usr/bin/env bash
# grade.sh <checkout-dir> -- markdown-it-py: IndexError at EOF in block terminator rules
set -u
set -f  # INSTALL_ARGS contains brackets; never glob
INSTALL_ARGS='-e .[testing,linkify]'
TARGET_TESTS='tests/test_fuzzer.py::test_gh_415_table_in_blockquote_at_eof_html_block tests/test_fuzzer.py::test_gh_415_table_in_blockquote_at_eof_heading'
MODULE_TESTS='tests/test_fuzzer.py tests/test_port tests/test_cmark_spec tests/test_api tests/test_tree.py'

if [ $# -ne 1 ] || [ ! -d "$1" ]; then
  echo "usage: $0 <checkout-dir>" >&2
  exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)
CO=$(cd "$1" && pwd)
VENV="$CO/.grade-venv"
PY="$VENV/bin/python"
export PATH="$VENV/bin:$PATH"  # tests may spawn console scripts of the package

# 1. overlay hidden tests (overwrites the agent's copies of these files)
cp -R "$HERE/hidden/." "$CO/"

# keep the grading venv out of the agent's git diff
if [ -d "$CO/.git" ]; then
  grep -qxF '.grade-venv/' "$CO/.git/info/exclude" 2>/dev/null || echo '.grade-venv/' >> "$CO/.git/info/exclude"
fi

# 2. idempotent venv + deps
cd "$CO" || exit 1
if [ ! -x "$PY" ]; then
  uv venv -q "$VENV" || { echo "FAIL setup: uv venv"; exit 1; }
fi
# shellcheck disable=SC2086
uv pip install -q -p "$PY" $INSTALL_ARGS || { echo "FAIL setup: dependency install"; exit 1; }

# 3. run tests
rc=0
run() {
  local label=$1; shift
  if "$PY" -m pytest -q -p no:cacheprovider "$@" >"$VENV/grade-$label.log" 2>&1; then
    echo "PASS $label"
  else
    echo "FAIL $label"
    tail -n 30 "$VENV/grade-$label.log"
    rc=1
  fi
}
# shellcheck disable=SC2086
run target $TARGET_TESTS
# shellcheck disable=SC2086
run module $MODULE_TESTS

if [ $rc -eq 0 ]; then echo "PASS overall"; else echo "FAIL overall"; fi
exit $rc
