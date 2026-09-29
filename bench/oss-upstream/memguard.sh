#!/usr/bin/env bash
# kill benchmark runs before the machine runs out of memory
while :; do
  a=$(free -g | awk '/Mem:/{print $7}'); s=$(free -g | awk '/Swap:/{print $4}')
  if [ "$a" -lt 6 ] || [ "$s" -lt 5 ]; then
    echo "$(date +%H:%M) LOW avail=${a}G swapfree=${s}G -> killing bench runs" >> ~/orbi-bench/memguard.log
    pkill -f '^bash ./queue.sh'; pkill -f '^bash ./run2.sh'
    for p in $(pgrep -f 'ORBI_CONFIG|/orbi-bench/runs/|obench-'); do [ "$p" != "$$" ] && kill $p 2>/dev/null; done
  fi
  sleep 20
done
