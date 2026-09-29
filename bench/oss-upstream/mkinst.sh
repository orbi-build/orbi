#!/usr/bin/env bash
# usage: mkinst.sh <repo:pyinfra|chainloop|fedify> <inst-name> <variant-dir>
# variant-dir holds: engine.toml (pi_* lines), prompt.md, prompt_review.md, skills (optional list file)
set -euo pipefail
R=$1; N=$2; V=$3
B=~/orbi-bench; I=$B/runs/$N; GH=xqliu/obench-$N
case $R in
  pyinfra) BASE=0d34e0e8cf191d67f91d47c7e3c4abc56c4c5b29; BR=3.x; TAG=v3.10.0 ;;
  chainloop) BASE=55c73e1ce1000155616885eccf9fa49cdcd841c3; BR=main; TAG= ;;
  fedify) BASE=2152dfc64f1c0b2aef1ff1488a7a33b0827853c5; BR=main; TAG= ;;
esac
rm -rf "$I"; mkdir -p "$I/snap" "$I/ws"
git -C $B/src/$R archive $BASE | tar -x -C "$I/snap"
cd "$I/snap"; git init -q -b $BR; git add -A; git -c user.name="Lawrence Liu" -c user.email=smartlitchi@gmail.com commit -q -m "base snapshot of $R at ${BASE:0:8}"
[ -n "$TAG" ] && git tag $TAG
gh repo create $GH --private --source . --push >/dev/null
[ -n "$TAG" ] && git push -q origin $TAG
gh api -X PUT repos/$GH/actions/permissions -F enabled=false >/dev/null
for l in ai-ready ai-in-progress ai-pr-opened ai-fix-needed ai-merged ai-blocked ai-awaiting-merge; do gh label create $l -R $GH >/dev/null 2>&1 || true; done
git clone -q git@github.com:$GH.git "$I/repo"
num=$(gh issue create -R $GH --title "$(cat $B/issues/$R.title)" --body-file $B/issues/$R.md --label ai-ready | grep -oE '[0-9]+$')
cat > "$I/orbi.toml" <<TOML
source_repos = ["$GH"]
repo_dir = "$I/repo"
deploy_home = "/home/xqianliu/orbi-deploy/orbi"
workspace_root = "$I/ws"
prompt = "$V/prompt.md"
prompt_review = "$V/prompt_review.md"
base_branch = "$BR"
max_concurrency = 1
context_files = []
$(cat $V/engine.toml)
TOML
[ -f $V/pathprefix ] && cp $V/pathprefix $I/
[ -f $V/env ] && cp $V/env $I/
echo "$N $GH issue=$num base=$BR"
