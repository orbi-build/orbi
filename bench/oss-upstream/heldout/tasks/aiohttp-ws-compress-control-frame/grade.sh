#!/usr/bin/env bash
# Usage: grade.sh <checkout dir>
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:?usage: grade.sh <checkout dir>}"
cd "$DIR" || { echo "FAIL: cannot cd $DIR"; exit 1; }
cp -R "$HERE/hidden/." "$DIR/"

EXCL="$(git rev-parse --git-path info/exclude)"; mkdir -p "$(dirname "$EXCL")"
grep -qxF ".venv/" "$EXCL" 2>/dev/null || echo ".venv/" >> "$EXCL"
git submodule update --init -q >/dev/null 2>&1 || true
export AIOHTTP_NO_EXTENSIONS=1
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || uv venv -q -p 3.13 .venv >/dev/null 2>&1 || { echo "FAIL: venv"; exit 1; }
uv pip install -q -p "$PY" -r requirements/test.txt -e . >/dev/null 2>&1 || { echo "FAIL: deps"; exit 1; }

# test_control_frame_with_rsv1 asserts a stricter RFC 7692 check that is outside the scope of the task.
DESEL=(--deselect tests/test_websocket_parser.py::test_control_frame_with_rsv1)
PT=("$PY" -m pytest -q -p no:cacheprovider --timeout=60 "${DESEL[@]}")
rc=0
if "${PT[@]}" tests/test_websocket_parser.py -k "compress or control or continuation or ping or pong"; then echo "PASS: target tests (websocket parser compression/control frames)"; else echo "FAIL: target tests"; rc=1; fi
if "${PT[@]}" tests/test_websocket_parser.py tests/test_websocket_writer.py tests/test_web_websocket.py tests/test_web_websocket_functional.py tests/test_client_ws.py tests/test_client_ws_functional.py; then echo "PASS: module tests (websocket)"; else echo "FAIL: module tests (websocket)"; rc=1; fi

if [ $rc -eq 0 ]; then echo "PASS: all checks"; else echo "FAIL: some checks failed"; fi
exit $rc
