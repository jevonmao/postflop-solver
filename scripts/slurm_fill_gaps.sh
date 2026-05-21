#!/usr/bin/env bash
#
# SLURM: fill in missing / partial spots for one or more matchups.
#
# Strategy: re-run the driver across the full tier on a single node. The
# driver's file-presence skip means already-complete spots cost ~ms each
# (just stat calls), so the wall-clock is dominated by the missing
# solves. With ~20 missing SRP spots this finishes in ~25–30 min on a
# 2-NUMA svl8 node; smaller for 3BP / 4BP.
#
# Submit:
#   MATCHUPS=SRP sbatch scripts/slurm_fill_gaps.sh
#   MATCHUPS=3BP sbatch scripts/slurm_fill_gaps.sh
#   MATCHUPS=SRP,3BP sbatch scripts/slurm_fill_gaps.sh    # both, sequential phases
#
# Pre-flight (run from a login node before submitting):
#   python3 scripts/find_missing.py SRP
#
# Recovery: safe to resubmit at any time. Atomic .tmp→rename writes mean
# kill -9 mid-spot cannot leave a half-written file.

# ---------- SLURM resource request ----------
#SBATCH --job-name=postflop-fill
#SBATCH --account=vision
#SBATCH --partition=svl
#SBATCH --qos=normal
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=72
#SBATCH --mem=128G
#SBATCH --time=01:00:00
#SBATCH --output=logs/slurm_fill_%j.out
#SBATCH --error=logs/slurm_fill_%j.err
#SBATCH --requeue

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
mkdir -p logs

# ---------- environment ----------
: "${CARGO_HOME:=/vision/u/jevon/.cargo}"
: "${RUSTUP_HOME:=/vision/u/jevon/.rustup}"
export CARGO_HOME RUSTUP_HOME
if [ -f "$CARGO_HOME/env" ]; then
    source "$CARGO_HOME/env"
elif [ -f "$HOME/.cargo/env" ]; then
    source "$HOME/.cargo/env"
fi
command -v cargo >/dev/null || { echo "ERROR: cargo not on PATH" >&2; exit 1; }

# Where the existing solves live (and where the driver will write the new ones).
# Must match the viewer's SOLVES_DIR and the path scanned by find_missing.py.
: "${OUT_DIR:=$SLURM_SUBMIT_DIR/solves}"
export OUT_DIR

TIER="${TIER:-full}"
MATCHUPS="${MATCHUPS:-SRP}"

# Keep combo-v2 emission on by default — matches the rest of the dataset.
: "${COMBO_DATA:=1}"
export COMBO_DATA

# On svl8 (503 GB RAM) there's no reason to ever trigger zstd compression of
# the solve buffers; raise the threshold past the heaviest SRP spot.
: "${COMPRESS_THRESHOLD_GB:=999}"
export COMPRESS_THRESHOLD_GB

echo "===================================================================="
echo "  Fill-gaps job $SLURM_JOB_ID on $(hostname)"
echo "  Matchups: $MATCHUPS    Tier: $TIER"
echo "  OUT_DIR : $OUT_DIR"
echo "  COMBO_DATA=$COMBO_DATA  COMPRESS_THRESHOLD_GB=$COMPRESS_THRESHOLD_GB"
echo "===================================================================="

# Pre-flight: print what's missing so the SLURM log records it.
if [ -f scripts/find_missing.py ]; then
    for m in $(echo "$MATCHUPS" | tr ',' ' '); do
        echo ""
        echo "--- find_missing.py $m ---"
        SOLVES_DIR="$OUT_DIR" python3 scripts/find_missing.py "$m" \
            > "logs/missing_${m}_${SLURM_JOB_ID}.txt" 2>&1 || true
        cat "logs/missing_${m}_${SLURM_JOB_ID}.txt"
    done
    echo ""
fi

exec ./scripts/run_production.sh "$TIER" "$MATCHUPS"
