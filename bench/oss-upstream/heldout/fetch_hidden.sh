#!/usr/bin/env bash
# Materialise each task's hidden/ tests from the upstream fix commit (FIX in meta.env).
# We don't vendor upstream test files; this fetches them at their original paths.
# usage: fetch_hidden.sh <tasks-dir> <cache-dir>
set -euo pipefail
T=${1:?tasks dir}; C=${2:?clone cache dir}; mkdir -p "$C"
for d in "$T"/*/; do
  n=$(basename "$d"); [ -f "$d/hidden.list" ] || continue
  ( . "$d/meta.env"
    S="$C/$n"; [ -d "$S/.git" ] || git clone -q --filter=blob:none "https://github.com/$REPO" "$S"
    git -C "$S" cat-file -e "$FIX^{commit}" 2>/dev/null || git -C "$S" fetch -q origin "$FIX"
    while read -r f; do mkdir -p "$d/hidden/$(dirname "$f")"; git -C "$S" show "$FIX:$f" > "$d/hidden/$f"; done < "$d/hidden.list"
    echo "$n: $(wc -l < "$d/hidden.list") file(s)" )
done
