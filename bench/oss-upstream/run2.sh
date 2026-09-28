#!/usr/bin/env bash
# usage: run.sh <inst-name> [max-minutes]
N=$1; MAX=${2:-120}
I=~/orbi-bench/runs/$N; GH=xqliu/obench-$N
export ORBI_CONFIG=$I/orbi.toml PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/bin:/bin"
[ -f $I/pathprefix ] && export PATH="$(cat $I/pathprefix):$PATH"
[ -f $I/env ] && set -a && . $I/env && set +a
start=$(date +%s)
while :; do
  labels=$(gh api repos/$GH/issues/1 --jq '[.labels[].name]|join(",")' 2>/dev/null)
  case "$labels" in *ai-merged*|*ai-blocked*) echo "DONE $N labels=$labels elapsed=$(( ($(date +%s)-start)/60 ))m"; exit 0;; esac
  [ $(( ($(date +%s)-start)/60 )) -ge $MAX ] && { echo "TIMEOUT $N labels=$labels"; exit 1; }
  (cd $I && timeout 7200 orbi >> $I/runner.log 2>&1)
  sleep 120
done
