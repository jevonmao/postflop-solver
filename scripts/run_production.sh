#!/usr/bin/env bash
#
# Production launcher for the HU 200 BB dataset driver on a multi-socket
# Intel Xeon system. Runs matchups in **sequential phases**; within each phase,
# launches N shards per NUMA node, each pinned with `numactl --physcpubind
# --membind` to a disjoint CPU subset of that node. All shards in a phase
# share OUT_DIR and stripe across the stratified-order flop list via
# SHARD_INDEX / SHARD_COUNT.
#
# Why phase by matchup: SRP is memory-bandwidth bound and saturates one
# socket's DRAM channels per solve — 1 shard / NUMA node is correct.
# 4BP/3BP are core-bound (working sets fit ~L3 / small RAM), so they get
# big wall-clock wins from oversubscribing the cores with more shards.
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
# Tunables (env vars, override at launch):
#   SHARDS_PER_NODE_4BP   default 8   (4BP fits in L3, core-bound)
#   SHARDS_PER_NODE_3BP   default 4   (1.5 GB / solve, mild bandwidth)
#   SHARDS_PER_NODE_SRP   default 1   (17 GB / solve, bandwidth-bound)
#   PHASE_ORDER           default "4BP,3BP,SRP"  (fast→slow for early feedback)
#
# Resumability: rerun with the same tier. Already-solved spots are skipped via
# file-presence check (atomic .tmp → rename writes). Killing the script and
# restarting is safe.
#
# Output:
#   data/solves/<matchup>/<idx>_<flop>.jsonl       — records
#   data/solves/<matchup>/<idx>_<flop>.meta        — solve stats per spot
#   logs/<matchup>_node<N>_shard<S>.log            — per-shard stdout

set -euo pipefail

TIER="${1:-full}"
MATCHUPS_ARG="${2:-4BP,3BP,SRP}"

# Per-matchup oversubscription defaults. Override via env.
SHARDS_PER_NODE_4BP="${SHARDS_PER_NODE_4BP:-8}"
SHARDS_PER_NODE_3BP="${SHARDS_PER_NODE_3BP:-4}"
SHARDS_PER_NODE_SRP="${SHARDS_PER_NODE_SRP:-1}"

# Order of matchup phases. Fast→slow by default so 4BP/3BP results are
# usable for downstream LLM-side iteration while SRP is still running.
PHASE_ORDER="${PHASE_ORDER:-4BP,3BP,SRP}"

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
echo "==> Detected $NUMA_NODES NUMA node(s)."

mkdir -p logs data/solves

# Expand CPU list (e.g. "0-17,36-53") into a space-separated list of CPU IDs,
# preserving the order numactl reports. Stored per-node in NODE_CPU_LIST[n].
declare -a NODE_CPU_LIST
declare -a NODE_CPU_COUNT
for n in $(seq 0 $((NUMA_NODES - 1))); do
    raw=$(numactl --hardware | awk -v node="node $n cpus:" '$0 ~ node { for (i=4; i<=NF; i++) printf "%s ", $i }')
    # raw fields are individual CPU IDs already (numactl --hardware format)
    NODE_CPU_LIST[$n]="$raw"
    NODE_CPU_COUNT[$n]=$(echo "$raw" | wc -w)
    echo "    node $n: ${NODE_CPU_COUNT[$n]} CPUs"
done

# Resolve which matchups to run, intersecting PHASE_ORDER with MATCHUPS_ARG.
IFS=',' read -ra REQUESTED <<< "$MATCHUPS_ARG"
IFS=',' read -ra ORDER     <<< "$PHASE_ORDER"
PHASES=()
for m in "${ORDER[@]}"; do
    for r in "${REQUESTED[@]}"; do
        if [ "$m" = "$r" ]; then PHASES+=("$m"); break; fi
    done
done
if [ "${#PHASES[@]}" -eq 0 ]; then
    echo "ERROR: no matchups left after intersecting PHASE_ORDER ($PHASE_ORDER) with arg ($MATCHUPS_ARG)" >&2
    exit 1
fi
echo "==> Phase order: ${PHASES[*]}"

# Given a CPU list (space-separated IDs), a shard index, and shard count,
# print a comma-separated subset for `numactl --physcpubind=`.
# Splits as evenly as possible (last shards get the remainder).
cpu_subset() {
    local list="$1" shard_idx="$2" shard_cnt="$3"
    local -a arr=($list)
    local total=${#arr[@]}
    local base=$(( total / shard_cnt ))
    local rem=$((  total % shard_cnt ))
    # Shards [0..rem) get base+1 cpus; rest get base.
    local start
    if [ "$shard_idx" -lt "$rem" ]; then
        start=$(( shard_idx * (base + 1) ))
        local len=$(( base + 1 ))
    else
        start=$(( rem * (base + 1) + (shard_idx - rem) * base ))
        local len=$base
    fi
    local end=$(( start + len ))
    local out=""
    for ((i=start; i<end; i++)); do
        if [ -z "$out" ]; then out="${arr[$i]}"; else out="$out,${arr[$i]}"; fi
    done
    echo "$out"
}

# Run one matchup phase: launches (shards_per_node × NUMA_NODES) shards in
# parallel, each numactl-pinned. Waits for all to finish before returning.
run_phase() {
    local matchup="$1" shards_per_node="$2"
    local total_shards=$(( shards_per_node * NUMA_NODES ))

    echo
    echo "========================================================="
    echo "==> Phase: $matchup   (${shards_per_node} shards/node × ${NUMA_NODES} nodes = ${total_shards} total)"
    echo "========================================================="

    local PIDS=() shard_idx=0
    local phase_start=$(date +%s)
    for n in $(seq 0 $((NUMA_NODES - 1))); do
        for s in $(seq 0 $((shards_per_node - 1))); do
            local cpus=$(cpu_subset "${NODE_CPU_LIST[$n]}" "$s" "$shards_per_node")
            local nthreads=$(echo "$cpus" | tr ',' '\n' | wc -l)
            local log="logs/${matchup}_node${n}_shard${s}.log"
            echo "    [$matchup] node $n shard $s   cpus=$cpus  threads=$nthreads   log=$log"
            (
                export SHARD_INDEX="$shard_idx"
                export SHARD_COUNT="$total_shards"
                export TIER="$TIER"
                export MATCHUPS="$matchup"
                export RAYON_NUM_THREADS="$nthreads"
                exec numactl --physcpubind="$cpus" --membind="$n" \
                    ./target/release/examples/dataset_driver
            ) > "$log" 2>&1 &
            PIDS+=("$!")
            shard_idx=$((shard_idx + 1))
        done
    done

    local exit_code=0
    for pid in "${PIDS[@]}"; do
        if ! wait "$pid"; then
            exit_code=1
            echo "WARN: $matchup shard PID $pid exited non-zero" >&2
        fi
    done
    local phase_end=$(date +%s)
    echo "==> Phase $matchup done in $((phase_end - phase_start)) seconds."
    return "$exit_code"
}

# ---------- run phases sequentially ----------
START_TS=$(date +%s)
OVERALL_EXIT=0
for matchup in "${PHASES[@]}"; do
    case "$matchup" in
        4BP) spn="$SHARDS_PER_NODE_4BP" ;;
        3BP) spn="$SHARDS_PER_NODE_3BP" ;;
        SRP) spn="$SHARDS_PER_NODE_SRP" ;;
        *)   echo "WARN: unknown matchup '$matchup', defaulting to 1 shard/node" >&2; spn=1 ;;
    esac
    if ! run_phase "$matchup" "$spn"; then
        OVERALL_EXIT=1
        echo "WARN: phase $matchup had failures; continuing with next phase" >&2
    fi
done

END_TS=$(date +%s)
echo
echo "==> All phases done in $((END_TS - START_TS)) seconds."
echo "==> Auditing output..."
cargo run --release --example verify_dataset

exit "$OVERALL_EXIT"
