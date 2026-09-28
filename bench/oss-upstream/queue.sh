#!/usr/bin/env bash
# usage: queue.sh <queue-file> <max-concurrent>; add lines with: flock <queue-file>.lock -c 'echo ... >> file'
Q=$1; K=$2; B=~/orbi-bench
while :; do
  running=$(pgrep -af '^bash ./run2?.sh' | awk '{print $4}' | sort -u | wc -l)
  avail=$(free -g | awk '/Mem:/{print $7}')
  gql=$(gh api rate_limit --jq .resources.graphql.remaining 2>/dev/null || echo 0)
  if [ "$running" -lt "$K" ] && [ "$avail" -ge 6 ] && [ "$gql" -ge 1500 ]; then
    line=$(flock $Q.lock -c "head -1 $Q; sed -i 1d $Q")
    if [ -n "$line" ]; then
      set -- $line; echo "$line" >> $Q.done
      if (cd $B && ./mkinst.sh $1 $2 $B/variants/$3 >> $B/queue.log 2>&1); then
        (cd $B && nohup ./run2.sh $2 180 > runs/$2/run.out 2>&1 &); echo "$(date +%H:%M) LAUNCH $2 gql=$gql" >> $B/queue.log
      else echo "$(date +%H:%M) MKINST-FAIL $2" >> $B/queue.log; fi
      sleep 30; continue
    fi
  fi
  sleep 60
done
