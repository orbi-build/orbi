#!/usr/bin/env bash
# two tables: core (code correct) and full (core + upstream-ready), pass/total
cd ~/orbi-bench/runs
declare -A P F T
for d in */; do n=${d%/}; [ -f $n/score.txt ] || continue
  r=${n%%-*}; v=$(echo $n | cut -d- -f2)
  T[$v,$r]=$(( ${T[$v,$r]:-0}+1 ))
  if grep -q 'RESULT exit=0' $n/score.txt; then P[$v,$r]=$(( ${P[$v,$r]:-0}+1 )); grep -q 'UPSTREAM exit=0' $n/score.txt && F[$v,$r]=$(( ${F[$v,$r]:-0}+1 )); fi
done
printf "%-5s | %-6s %-6s %-6s | %-6s %-6s %-6s\n" var py cl fe py cl fe
printf "%-5s | %-20s | %-20s\n" "" "core (code correct)" "full (+upstream)"
for v in v0 v1 v2 v4 v5 v6 v7 v8 v9 v10 v11 v12 v13 v14; do
  printf "%-5s |" $v; for r in py cl fe; do [ -n "${T[$v,$r]}" ] && printf " %-6s" "${P[$v,$r]:-0}/${T[$v,$r]}" || printf " %-6s" "-"; done
  printf " |"; for r in py cl fe; do [ -n "${T[$v,$r]}" ] && printf " %-6s" "${F[$v,$r]:-0}/${T[$v,$r]}" || printf " %-6s" "-"; done; echo; done
