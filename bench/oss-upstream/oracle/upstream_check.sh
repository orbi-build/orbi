#!/usr/bin/env bash
# usage: upstream_check.sh <dir> <repo> <base>  -> prints UPSTREAM PASS/FAIL lines, exit 0 if all pass
cd "$1" || exit 2; R=$2; BASE=$3
case $R in pyinfra) UP=1438;; chainloop) UP=3481;; fedify) UP=1098;; esac
CONV='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: .+'
fail=0; n=0
for c in $(git rev-list --no-merges $BASE..HEAD); do n=$((n+1))
  subj=$(git log -1 --format=%s $c); body=$(git log -1 --format=%B $c)
  refs=$(echo "$body" | grep -oE '(^|[^A-Za-z0-9/_-])#[0-9]+' | grep -oE '[0-9]+' | grep -vx $UP)
  [ -n "$refs" ] && { echo "FAIL upstream: fork-local ref #$(echo $refs|tr ' ' ',') in ${c:0:7}"; fail=1; }
  case $R in
    pyinfra) echo "$subj" | grep -qE "$CONV" || { echo "FAIL upstream: not conventional '${subj:0:50}'"; fail=1; } ;;
    chainloop) echo "$subj" | grep -qE "$CONV" || { echo "FAIL upstream: not conventional '${subj:0:50}'"; fail=1; }
               echo "$body" | grep -qE '^Signed-off-by: .+ <.+>' || { echo "FAIL upstream: no DCO Signed-off-by in ${c:0:7}"; fail=1; }
               echo "$body" | grep -qE '^Assisted-by: .+' || { echo "FAIL upstream: no Assisted-by in ${c:0:7}"; fail=1; } ;;
    fedify) echo "$subj" | grep -qE "$CONV" && { echo "FAIL upstream: forbidden conventional prefix '${subj:0:50}'"; fail=1; }
            echo "$body" | grep -qE '^Assisted-by: [^:]+:.+' || { echo "FAIL upstream: no Assisted-by AGENT:MODEL in ${c:0:7}"; fail=1; } ;;
  esac
done
[ $n -eq 0 ] && { echo "FAIL upstream: no commits"; fail=1; }
[ $fail -eq 0 ] && echo "PASS upstream ($n commits)"
exit $fail
