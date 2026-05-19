#!/usr/bin/env bash
#
# Production launcher for the HU 200 BB dataset driver on a multi-socket
# Intel Xeon system. Launches one driver process per NUMA node, pinned with
# `numactl`. Both processes share the same OUT_DIR and stripe across the
# stratified-order flop list via SHARD_INDEX / SHARD_COUNT.
#
# Designed for a 2-socket Xeon Gold 5220 (72 logical CPUs, 2 NUMA nodes,
# AVX-512). Adapts to other NUMA topologies via the autodetected node list.
#
# Usage:
#   ./scripts/run_production.sh smoke      # 100 flops × 3 matchups
#   ./scripts/run_production.sh medium     # 500 flops × 3 matchups
#   ./scripts/run_production.sh full       # 1755 flops × 3 matchups
#   ./scripts/run_production.sh full SRP   # just SRP (any matchup subset)
#
# Resumability: rerun with the same tier. Already-solved spots are skipped via
# file-presence check (atomic .tmp → rename writes). Killing the script and
# restarting is safe.
#
# Output:
#   data/solves/<matchup>/<idx>_<flop>.jsonl       — records
#   data/solves/<matchup>/<idx>_<flop>.meta        — solve stats per spot
#   logs/shard_<i>.log                             — per-shard stdout

set -euo pipefail

TIER="${1:-full}"
MATCHUPS="${2:-SRP,3BP,4BP}"

cd "$(dirname "$0")/.."

# ---------- prereqs ----------
if ! command -v numactl >/dev/null; then
    echo "ERROR: numactl not installed. apt-get install numactl   (or equivalent)" >&2
    exit 1
fi
if ! command -v cargo >/dev/null; then
    echo "ERROR: cargo not on PATH. Source ~/.cargo/env or install rustup." >&2
    exit 1
fi

# ---------- build (with native CPU flags from .cargo/config.toml) ----------
echo "==> Building dataset_driver in release mode (target-cpu=native)..."
cargo build --release --example dataset_driver

# Sanity-check the binary picked up AVX-512 on this machine.
BIN="target/release/examples/dataset_driver"
if [ -f "$BIN" ]; then
    AVX512_COUNT=$(objdump -d "$BIN" 2>/dev/null | grep -cE '\bv(add|mul|fmadd)p[sd]\b.*zmm[0-9]+' || true)
    AVX2_COUNT=$(objdump -d "$BIN" 2>/dev/null | grep -cE '\bv(add|mul|fmadd)p[sd]\b.*ymm[0-9]+' || true)
    echo "    AVX-512 instrs: $AVX512_COUNT     AVX2/AVX instrs: $AVX2_COUNT"
fi

# ---------- prereq files ----------
if [ ! -f data/hu_200bb_ranges.txt ]; then
    echo "ERROR: data/hu_200bb_ranges.txt missing. Paste preflop ranges first." >&2
    exit 1
fi
if [ ! -f data/canonical_flops_stratified.txt ]; then
    echo "==> Generating canonical_flops files..."
    cargo run --release --example canonical_flops
fi

# ---------- NUMA topology detection ----------
NUMA_NODES=$(numactl --hardware | awk '/^available:/{ print $2 }')
if [ -z "$NUMA_NODES" ]; then
    echo "ERROR: numactl --hardware failed to report node count." >&2
    exit 1
fi
echo "==> Detected $NUMA_NODES NUMA node(s). Launching one driver per node."

mkdir -p logs data/solves

# Threads per shard: count CPUs on each NUMA node, use them all (including HT).
declare -a CPUS_PER_NODE
TOTAL_THREADS=0
for n in $(seq 0 $((NUMA_NODES - 1))); do
    # CPU list like "0-17,36-53" → count
    list=$(numactl --hardware | awk -v node="node $n cpus:" '$0 ~ node { for (i=4; i<=NF; i++) printf "%s ", $i }')
    count=$(echo "$list" | tr ',' '\n' | awk -F- '{ if (NF==2) print $2-$1+1; else if (NF==1 && $1!="") print 1 }' | paste -sd+ | bc)
    CPUS_PER_NODE[$n]=$count
    TOTAL_THREADS=$((TOTAL_THREADS + count))
    echo "    node $n: $count CPUs"
done

# ---------- launch shards ----------
PIDS=()
START_TS=$(date +%s)
for n in $(seq 0 $((NUMA_NODES - 1))); do
    LOG="logs/shard_${n}.log"
    echo "==> Launching shard $n on NUMA node $n  (RAYON_NUM_THREADS=${CPUS_PER_NODE[$n]})"
    echo "    log: $LOG"
    (
        export SHARD_INDEX="$n"
        export SHARD_COUNT="$NUMA_NODES"
        export TIER="$TIER"
        export MATCHUPS="$MATCHUPS"
        export RAYON_NUM_THREADS="${CPUS_PER_NODE[$n]}"
        # numactl: pin both CPUs *and* memory to this NUMA node so allocations
        # stay local (huge for memory-bandwidth-bound solver hot path).
        exec numactl --cpunodebind="$n" --membind="$n" \
            ./target/release/examples/dataset_driver
    ) > "$LOG" 2>&1 &
    PIDS+=("$!")
done

echo
echo "==> Shards launched: ${PIDS[*]}"
echo "    Tail per-shard progress with:"
for n in $(seq 0 $((NUMA_NODES - 1))); do
    echo "      tail -f logs/shard_${n}.log"
done
echo
echo "==> Waiting for all shards to finish. (Ctrl-C is safe — atomic writes)"

# Wait, propagate exit code
EXIT_CODE=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then
        EXIT_CODE=1
        echo "WARN: shard PID $pid exited non-zero" >&2
    fi
done

END_TS=$(date +%s)
echo
echo "==> All shards done in $((END_TS - START_TS)) seconds."
echo "==> Auditing output..."
cargo run --release --example verify_dataset

exit "$EXIT_CODE"
