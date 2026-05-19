#!/usr/bin/env bash
# Quick status snapshot — count completed spots and projected ETA without
# parsing the running shard logs. Safe to run any time.

set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/solves}"

printf "%-6s %8s %8s %8s\n" "match" "done" "smoke" "med"
for m in SRP 3BP 4BP; do
    if [ -d "$OUT_DIR/$m" ]; then
        DONE=$(find "$OUT_DIR/$m" -name "*.meta" | wc -l)
    else
        DONE=0
    fi
    # smoke = 100 stratified positions, medium = 500
    # We can't tell which without parsing the stratified file, so just print done.
    printf "%-6s %8s\n" "$m" "$DONE"
done

# If a shard log exists, show the tail of each.
if compgen -G "logs/shard_*.log" > /dev/null; then
    echo
    echo "==> last 3 lines of each shard log:"
    for f in logs/shard_*.log; do
        echo "--- $f ---"
        tail -n 3 "$f"
    done
fi
