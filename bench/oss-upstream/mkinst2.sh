#!/usr/bin/env bash
# held-out tasks. usage: mkinst2.sh <task> <inst-name> <variant-dir>
set -euo pipefail
T=$1; N=$2; V=$3
B=~/orbi-bench; TD=$B/tasks/$T; I=$B/runs/$N; GH=xqliu/obench-$N
. $TD/meta.env
S=$B/src/ho-$T
[ -d $S/.git ] || git clone -q --filter=blob:none https://github.com/$REPO $S
git -C $S cat-file -e $BASE^{commit} 2>/dev/null || git -C $S fetch -q origin $BASE
rm -rf "$I"; mkdir -p "$I/snap" "$I/ws"
if git -C $S show $BASE:.gitmodules >/dev/null 2>&1; then
  W=$S.wt; [ -d $W ] || git -C $S worktree add -q --detach $W $BASE
  git -C $W checkout -q --detach $BASE && git -C $W submodule update -q --init --recursive
  (cd $W && tar --exclude=.git -cf - .) | tar -x -C "$I/snap"; rm -f "$I/snap/.gitmodules"
else
  git -C $S archive $BASE | tar -x -C "$I/snap"
fi
cd "$I/snap"; rm -rf .github/workflows
git init -q -b $BR; git add -A -f; git -c user.name="Lawrence Liu" -c user.email=smartlitchi@gmail.com commit -q -m "base snapshot"
gh repo create $GH --private --source . --push >/dev/null
gh api -X PUT repos/$GH/actions/permissions -F enabled=false >/dev/null
for l in ai-ready ai-in-progress ai-pr-opened ai-fix-needed ai-merged ai-blocked ai-awaiting-merge; do gh label create $l -R $GH >/dev/null 2>&1 || true; done
git clone -q git@github.com:$GH.git "$I/repo"
num=$(gh issue create -R $GH --title "$(cat $TD/title)" --body-file $TD/issue.md --label ai-ready | grep -oE '[0-9]+$')
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
G=$B/guard/bin; if [ -f $V/pathprefix ]; then echo "$G:$(cat $V/pathprefix)" > $I/pathprefix; else echo "$G" > $I/pathprefix; fi
[ -f $V/env ] && cp $V/env $I/
echo "$T" > $I/task
echo "$N $GH issue=$num base=$BR"
