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

# `sort -V` is not portable across the BSD/GNU split this script must
# live on, so compare dotted version triples component by component in
# pure bash. Only Node's plain `vMAJOR.MINOR.PATCH` output is expected.
node_version_at_least() {
  local have want i
  local IFS=.
  read -r -a have <<<"${1#v}"
  read -r -a want <<<"$2"
  for i in 0 1 2; do
    [ "${have[i]:-0}" -eq "${want[i]:-0}" ] && continue
    [ "${have[i]:-0}" -lt "${want[i]:-0}" ] && return 1
    return 0
  done
  return 0
}

# A vanilla macOS has no `timeout` (coreutils ships only via Homebrew,
# as gtimeout — Issue #868), so a bare `timeout N …` aborts the whole
# install with `command not found` under set -e. When `timeout` exists
# every timed step keeps its bound; when it does not, the step simply
# runs without one.
with_timeout() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$@"
  else
    shift
    "$@"
  fi
}

# The scheduler is platform state, not a package the user can add:
# Orbi schedules its runner through launchd on macOS and systemd on
# Linux (Issue #849). A machine without its platform scheduler cannot
# run Orbi, and the error must say so plainly instead of reporting a
# bare missing command.
SCHEDULER_ISSUE_URL="https://github.com/orbi-build/orbi/issues/849"
case "$(uname -s)" in
  Darwin)
    SCHEDULER_BIN=launchctl
    SCHEDULER_NAME=launchd
    ;;
  Linux)
    SCHEDULER_BIN=systemctl
    SCHEDULER_NAME=systemd
    ;;
  *)
    printf 'orbi install: unsupported platform: %s\n' "$(uname -s)" >&2
    printf 'Orbi supports Linux (systemd) and macOS (launchd).\n' >&2
    printf 'Platform support: %s\n' "$SCHEDULER_ISSUE_URL" >&2
    exit 1
    ;;
esac
if ! command -v "$SCHEDULER_BIN" >/dev/null 2>&1; then
  printf 'orbi install: %s not found — this machine has no %s scheduler.\n' \
    "$SCHEDULER_BIN" "$SCHEDULER_NAME" >&2
  printf 'This is a platform limitation, not a missing package: Orbi needs\n' >&2
  printf 'launchd on macOS or systemd on Linux to schedule its runner.\n' >&2
  printf 'Platform support and macOS status: %s\n' "$SCHEDULER_ISSUE_URL" >&2
  exit 1
fi

# git is needed before anything can be installed. The remaining commands are
# setup prerequisites; report them here rather than failing halfway through.
require_command git
require_command gh
require_command curl

if ! command -v uv >/dev/null 2>&1; then
  printf 'orbi install: uv not found; installing it with the official installer\n' >&2
  # BSD mktemp (macOS) requires a template; the bare GNU call aborts
  # the whole install under set -e there (caught by the macOS
  # compatibility workflow, Issue #894).
  uv_installer=$(mktemp "${TMPDIR:-/tmp}/orbi-uv-installer.XXXXXXXX")
  trap 'rm -f "$uv_installer"' EXIT
  with_timeout 120 curl -LsSf https://astral.sh/uv/install.sh -o "$uv_installer"
  with_timeout 120 sh "$uv_installer"
  rm -f "$uv_installer"
  trap - EXIT
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
require_command uv

# Pi is the coding agent the Runner drives. Like uv it is a single
# user-scope CLI with an official install command, so the installer
# provides it (Issue #1080) instead of leaving the install "successful"
# with every later tick failing at the first Pi call. Node is Pi's
# engine and stays the user's prerequisite, the way gh is: a Node
# toolchain is a system-level decision this script does not make. The
# floor is Pi's own `engines` requirement.
PI_NODE_FLOOR="22.19.0"
PI_PACKAGE="@earendil-works/pi-coding-agent"
PI_REPO_URL="https://github.com/earendil-works/pi"
NODE_INSTALL_URL="https://nodejs.org/en/download"

if pi --version >/dev/null 2>&1; then
  printf 'orbi install: pi already installed; skipping\n' >&2
else
  if ! command -v node >/dev/null 2>&1; then
    printf 'orbi install: required command missing: node\n' >&2
    printf 'Pi needs Node >= %s; install Node first: %s\n' \
      "$PI_NODE_FLOOR" "$NODE_INSTALL_URL" >&2
    printf 'Node is a system-level prerequisite this installer leaves to you.\n' >&2
    exit 1
  fi
  node_version="$(node --version)"
  if ! node_version_at_least "$node_version" "$PI_NODE_FLOOR"; then
    printf 'orbi install: node %s found; Pi needs Node >= %s\n' \
      "$node_version" "$PI_NODE_FLOOR" >&2
    printf 'Install Node from %s and run this script again.\n' "$NODE_INSTALL_URL" >&2
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    printf 'orbi install: required command missing: npm (it ships with Node)\n' >&2
    printf 'npm is what installs Pi (%s); Pi details: %s\n' \
      "$PI_PACKAGE" "$PI_REPO_URL" >&2
    exit 1
  fi
  printf 'orbi install: pi not found; installing it with npm\n' >&2
  with_timeout 300 npm install -g --ignore-scripts @earendil-works/pi-coding-agent
  if ! pi --version >/dev/null 2>&1; then
    printf 'orbi install: the pi install did not produce a working `pi --version`\n' >&2
    printf 'a broken or too-old Node is the usual cause; Pi details: %s\n' \
      "$PI_REPO_URL" >&2
    exit 1
  fi
fi

mkdir -p "$ORBI_HOME"
if [ -e "$ORBI_SRC/.git" ]; then
  : # Keep the existing checkout and make setup idempotent.
elif [ -e "$ORBI_SRC" ]; then
  printf 'orbi install: %s exists but is not a git checkout\n' "$ORBI_SRC" >&2
  exit 1
else
  with_timeout 300 git clone git@github.com:orbi-build/orbi.git "$ORBI_SRC"
fi

cd "$ORBI_SRC"
with_timeout 300 uv tool install --force --reinstall --editable .
if [ ! -e orbi.toml ]; then
  cp src/orbi/example_config.toml orbi.toml
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
  sed -i.bak "s#OWNER/PILOT-REPO#$source_repo#; /OWNER\\/BACKLOG-REPO/d" orbi.toml && rm -f orbi.toml.bak
fi

# uv's tool bin directory may not be in PATH in the shell running curl|bash.
export PATH="$HOME/.local/bin:$PATH"
printf 'orbi install: starting interactive setup\n' >&2
orbi setup </dev/tty
