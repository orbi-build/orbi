#!/usr/bin/env bash
# usage: score2.sh <inst> -> runs/<inst>/score.txt (held-out task)
N=$1; B=~/orbi-bench; I=$B/runs/$N; GH=xqliu/obench-$N; T=$(cat $I/task); TD=$B/tasks/$T
. $TD/meta.env
D=$B/score/$N; rm -rf $D; git clone -q git@github.com:$GH.git $D; cd $D
BASEC=$(git rev-list --max-parents=0 HEAD | tail -1); src="merged $BR"
if git diff --quiet $BASEC HEAD; then
  pr=$(gh pr list -R $GH --state open --json headRefName -q '.[0].headRefName'); [ -n "$pr" ] && { git fetch -q origin $pr && git checkout -q FETCH_HEAD; src="open PR $pr"; } || src="no change"
fi
{ echo "source: $src ($(git rev-parse --short HEAD))"; echo "labels: $(gh issue view 1 -R $GH --json labels -q '[.labels[].name]|join(",")')";
  echo "diff: $(git diff --shortstat $BASEC HEAD)"
  timeout 1800 bash $TD/grade.sh $D; echo "RESULT exit=$?"; } > $I/score.txt 2>&1
tail -5 $I/score.txt
