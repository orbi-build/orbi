#!/bin/bash
# Orbi container bootstrap oneshot (community-maintained, Issue #264).
# Runs as a system service at every container start: bring up the `orbi`
# user's systemd user manager, wait for its bus, then run the official
# idempotent `orbi setup` as that user. After it succeeds, the enabled
# `orbi@1.timer` fires a runner tick every 5 minutes — the same
# mechanics as a host systemd deployment.
set -euo pipefail

UID_ORBI=$(id -u orbi)
RUNTIME_DIR="/run/user/$UID_ORBI"

install -d -m 700 -o orbi -g orbi "$RUNTIME_DIR"
systemctl start "user@$UID_ORBI.service"

# Fail fast, bounded: a user manager that never comes up is a broken
# container, not something to wait on forever (Issue #95 contract).
for _ in $(seq 1 30); do
  if runuser -u orbi -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
      systemctl --user is-active default.target >/dev/null 2>&1; then
    MANAGER_UP=1
    break
  fi
  sleep 1
done
[ "${MANAGER_UP:-0}" = 1 ] \
  || { echo "orbi-container-setup: the user manager did not come up" >&2; exit 1; }

echo "orbi-container-setup: running orbi setup (official idempotent initialization)"
exec runuser -u orbi -- env \
  HOME=/home/orbi \
  PATH="/home/orbi/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  orbi setup
