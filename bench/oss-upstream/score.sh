#!/usr/bin/env bash
# usage: score.sh <inst>  -> writes runs/<inst>/score.txt
N=$1; I=~/orbi-bench/runs/$N; GH=xqliu/obench-$N
case $N in py-*) R=pyinfra; BR=3.x;; cl-*) R=chainloop; BR=main;; fe-*) R=fedify; BR=main;; esac
D=~/orbi-bench/score/$N; rm -rf $D; git clone -q git@github.com:$GH.git $D
cd $D
BASE=$(git rev-list --max-parents=0 HEAD | tail -1)
src="merged $BR"
if git diff --quiet $BASE HEAD; then
  pr=$(gh pr list -R $GH --state open --json headRefName -q '.[0].headRefName'); [ -n "$pr" ] && { git fetch -q origin $pr && git checkout -q FETCH_HEAD; src="open PR $pr"; } || src="no change"
fi
[ $R = fedify ] && cp /tmp/claude-1000/-home-xqianliu-Projects-orbi/7f781b44-47a6-4929-88f4-74dd56ea7b9e/scratchpad/fedify/packages/vocab/src/vocab.ts packages/vocab/src/ 2>/dev/null
[ $R = pyinfra ] && git tag -f v3.10.0 $BASE >/dev/null 2>&1
{ echo "source: $src ($(git rev-parse --short HEAD))"; echo "labels: $(gh issue view 1 -R $GH --json labels -q '[.labels[].name]|join(",")')";
  if [ $R = fedify ]; then ~/orbi-bench/oracle/fedify/score.sh $D $BASE; else ~/orbi-bench/oracle/$R/score.sh $D; fi; echo "RESULT exit=$?"; ~/orbi-bench/oracle/upstream_check.sh $D $R $BASE; echo "UPSTREAM exit=$?"; } > $I/score.txt 2>&1
cat $I/score.txt
