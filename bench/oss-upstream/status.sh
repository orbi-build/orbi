#!/usr/bin/env bash
for d in ~/orbi-bench/runs/*/; do n=$(basename $d); l=$(gh issue view 1 -R xqliu/obench-$n --json labels -q '[.labels[].name]|join(",")' 2>/dev/null); p=$(tail -1 $d/runner.log 2>/dev/null | grep -oE 'role=[a-z]+ phase=[a-z_]+' ); printf "%-10s %-38s %s %s\n" $n "$l" "$p" "$(cat $d/run.out 2>/dev/null | tail -1)"; done
