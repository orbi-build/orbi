#!/usr/bin/env bash
# Install Orbi from its source checkout and run the interactive setup.
set -euo pipefail

ORBI_HOME="${ORBI_HOME:-$HOME/.orbi}"
ORBI_SRC="$ORBI_HOME/src"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'orbi install: required command missing: %s\n' "$1" >&2
    printf 'Install it and run this script again.\n' >&2
    exit 1
  fi
}

# git is needed before anything can be installed. The remaining commands are
# setup prerequisites; report them here rather than failing halfway through.
require_command git
require_command gh
require_command systemctl
require_command curl

if ! command -v uv >/dev/null 2>&1; then
  printf 'orbi install: uv not found; installing it with the official installer\n' >&2
  uv_installer=$(mktemp)
  trap 'rm -f "$uv_installer"' EXIT
  timeout 120 curl -LsSf https://astral.sh/uv/install.sh -o "$uv_installer"
  timeout 120 sh "$uv_installer"
  rm -f "$uv_installer"
  trap - EXIT
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
require_command uv

mkdir -p "$ORBI_HOME"
if [ -e "$ORBI_SRC/.git" ]; then
  : # Keep the existing checkout and make setup idempotent.
elif [ -e "$ORBI_SRC" ]; then
  printf 'orbi install: %s exists but is not a git checkout\n' "$ORBI_SRC" >&2
  exit 1
else
  timeout 300 git clone git@github.com:orbi-build/orbi.git "$ORBI_SRC"
fi

cd "$ORBI_SRC"
timeout 300 uv tool install --force --reinstall --editable .
if [ ! -e orbi.toml ]; then
  cp .orbi.example.toml orbi.toml
fi

# The example intentionally uses placeholders. Collect the first task-pool
# repo before setup validates GitHub access; read from the terminal because
# stdin is the installer itself in a curl | bash invocation.
if grep -q 'OWNER/PILOT-REPO' orbi.toml; then
  if [ ! -r /dev/tty ]; then
    printf 'orbi install: cannot collect the GitHub repository without a terminal\n' >&2
    exit 1
  fi
  printf 'GitHub task-pool repository (OWNER/REPO): ' >&2
  IFS= read -r source_repo </dev/tty
  if [[ ! "$source_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'orbi install: invalid GitHub repository: %s\n' "$source_repo" >&2
    exit 1
  fi
  sed -i "s#OWNER/PILOT-REPO#$source_repo#; /OWNER\\/BACKLOG-REPO/d" orbi.toml
fi

# uv's tool bin directory may not be in PATH in the shell running curl|bash.
export PATH="$HOME/.local/bin:$PATH"
printf 'orbi install: starting interactive setup\n' >&2
orbi setup </dev/tty
