#!/usr/bin/env bash
# loops forever: score any finished run that has no score yet; print one line per scored run
while :; do
  for d in ~/orbi-bench/runs/*/; do n=$(basename $d)
    [ -f $d/score.txt ] && continue
    grep -qE '^(DONE|TIMEOUT)' $d/run.out 2>/dev/null || continue
    ~/orbi-bench/score.sh $n > /dev/null 2>&1
    echo "SCORED $n: $(grep -E '^(FAIL|RESULT|source)' $d/score.txt | tr '\n' ' ')"
  done
  sleep 60
done
