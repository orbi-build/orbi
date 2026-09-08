#!/usr/bin/env bash
set -euo pipefail

# The release test command is the repository's contributor-facing contract.
# Keep its toolchain setup here rather than making the Orbi runner Python-only.
venv_dir="${TMPDIR:-/tmp}/orbi-release-test.$$"
trap 'rm -rf "$venv_dir"' EXIT
uv venv "$venv_dir"
uv pip install --python "$venv_dir/bin/python" pytest coverage pyyaml
mkdir -p .orbi
COVERAGE_FILE=.orbi/.coverage "$venv_dir/bin/python" -m coverage run --branch -m pytest tests/ -q
COVERAGE_FILE=.orbi/.coverage "$venv_dir/bin/python" -m coverage report --show-missing
COVERAGE_FILE=.orbi/.coverage "$venv_dir/bin/python" tools/coverage_gate.py .orbi/.coverage
