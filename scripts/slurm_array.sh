#!/usr/bin/env bash
#
# SLURM job-array driver for the HU 200 BB dataset pipeline.
#
# Pattern: each array task gets a disjoint contiguous slice of the
# stratified-order flop list (via FLOP_START / FLOP_LIMIT), then runs
# ./scripts/run_production.sh on its allocated node. The local script
# does NUMA-aware per-matchup oversubscription within its slice.
# Cross-task coordination is via shared filesystem + atomic .tmp→rename
# writes + file-presence skip — no explicit messaging needed.
#
# Submit:
#   sbatch scripts/slurm_array.sh                # full, all matchups
#   MATCHUPS=4BP sbatch scripts/slurm_array.sh   # just 4BP
#   sbatch --array=0-39%10 scripts/slurm_array.sh   # 40 slices, 10 at a time
#
# Resume after preemption / failure:
#   Just resubmit. File-presence skip means already-done spots cost ~0.
#
# Fill in the four `FIXME` lines below for your cluster before first use.

# ---------- SLURM resource request ----------
#SBATCH --job-name=postflop-dataset
#SBATCH --account=FIXME_GROUP_ACCOUNT
#SBATCH --partition=FIXME_PARTITION
#SBATCH --array=0-19                  # 20 slices × ~88 flops/slice (tune to fit walltime)
#SBATCH --nodes=1                     # one node per array task
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=72            # all logical CPUs on a 2-socket Xeon Gold 5220
#SBATCH --mem=128G                    # SRP peak ~17 GB × 2 NUMA shards + headroom
#SBATCH --time=12:00:00               # FIXME: see "Sizing" section in CLAUDE.md
#SBATCH --output=logs/slurm_%A_%a.out # %A = array job id, %a = task id
#SBATCH --error=logs/slurm_%A_%a.err
#SBATCH --requeue                     # auto-resubmit on preemption (resume-safe)

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
mkdir -p logs

# ---------- environment ----------
# Pick up cargo/rustup if installed in user dir. Adjust for your cluster.
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

# ---------- slice math ----------
# Each task takes WINDOW consecutive positions in stratified order.
# Last task may take fewer; FLOP_LIMIT extending past 1755 is clamped by the driver.
TOTAL_FLOPS=1755
ARRAY_SIZE="${SLURM_ARRAY_TASK_COUNT:-1}"
WINDOW=$(( (TOTAL_FLOPS + ARRAY_SIZE - 1) / ARRAY_SIZE ))

export TIER=full
export FLOP_START=$(( SLURM_ARRAY_TASK_ID * WINDOW ))
export FLOP_LIMIT=$WINDOW

echo "===================================================================="
echo "  SLURM task $SLURM_ARRAY_JOB_ID.$SLURM_ARRAY_TASK_ID on $(hostname)"
echo "  Array size: $ARRAY_SIZE   Window: stratified[$FLOP_START..$((FLOP_START+FLOP_LIMIT)))"
echo "  Matchups:   ${MATCHUPS:-4BP,3BP,SRP}"
echo "  CPUs:       $SLURM_CPUS_PER_TASK     Memory: $(scontrol show job $SLURM_JOB_ID | awk -F= '/mem=/{print $3; exit}')"
echo "===================================================================="

# Pass through MATCHUPS if set, else default.
exec ./scripts/run_production.sh "$TIER" "${MATCHUPS:-4BP,3BP,SRP}"
