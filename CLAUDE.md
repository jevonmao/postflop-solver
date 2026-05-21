# postflop-solver — engineering notes

A Rust library implementing Discounted CFR for postflop Texas Hold'em. Used here as the backend for generating a supervised dataset for a heads-up 200 BB poker LLM. The library is solver-only — it does not solve preflop; ranges are inputs.

## Build / run

- **Rust ≥ 1.95 build gotcha**: `src/action_tree.rs` (~L390–410) has a pattern `&*(*node).children[idx].lock()` through a `*const ActionTreeNode`. Rust 1.95 promoted "implicit autoref through raw pointer deref" to a hard error. Fix: bind `let children = &(*node).children;` first, then index. Fresh clones won't compile on current stable without this. Already applied in this working copy.
- Native CPU + fat LTO are configured in `.cargo/config.toml` and `Cargo.toml` `[profile.release]`. Keep them on, but don't expect speedups from tuning them — see perf note below.
- Always build with `--release`. Debug builds are orders of magnitude slower.
- Feature flags: `bincode`/`rayon` on by default. `zstd` is opt-in via `--features zstd` and is required for the `file_io` example and compressed dataset persistence.

## Examples in this repo

All runnable as `cargo run --release --example <name>`. Some take env vars.

| Example | Purpose |
|---|---|
| `basic` | Library smoke-test — single solve, queries strategy at a few nodes. Start here when verifying the build. |
| `file_io` | Save/load a solved tree with bincode + zstd. Needs `--features zstd`. |
| `node_locking` | Demonstrates locking a node's strategy. Verification-only, prints nothing on success. |
| `btn_vs_bb_100bb` | Realistic 100 BB BTN-vs-BB SRP solve with full equity/EV/strategy reporting at root. Useful tutorial spot. |
| `throughput_bench` | Solves representative river/turn/flop spots and prints projected throughput. |
| `hu_200bb_bench` | The HU 200 BB benchmark across SRP / 3BP / 4BP for two flops each. Memory + time per spot. |
| `limp_pot_bench` | Single-flop limped-pot (SPR ~99) benchmark. `FLOP=<board>` env var. Results in `LIMP_POT_BENCHMARK.md`: ~46 GB / ~253 s — heaviest HU spot. |
| `srp_speedup_bench` | A/B/C/D matrix comparing bet-tree complexity × exploitability target on the heaviest SRP spot. Source of the "rich-flop / lean-elsewhere / 2% target" recommendation. |
| `range_advantage_demo` | At-root range/nut/equity-bucket extraction. Shows that derived features track solver behavior on contrasting boards (Kh7d2c IP-favored vs Th9d8h roughly even). |
| `tree_walker_demo` | Full DFS over a solved game tree emitting one JSON record per decision node with strategy label + range-advantage features. Test it on small spots first via `SPOT=4bp` (~1.3s) before `SPOT=3bp` (~17s). |
| `canonical_flops` | Generates `data/canonical_flops.txt` (sorted, 1755 entries) and `data/canonical_flops_stratified.txt` (stratified order — tier-ready). |
| `verify_hu_ranges` | Loads `data/hu_200bb_ranges.txt` and prints a table of every non-empty range with combo count + total weight. Run after editing the template. |
| `solve_srp_real_ranges` | End-to-end smoke: loads ranges, solves one SRP flop, prints root-node range/nut advantages + BB strategy. `FLOP=<board>` env var. |
| `dataset_driver` | The production driver. Iterates `(matchup × canonical_flop)` in stratified order, writes one JSONL of decision-node records per spot. Resumable. See pipeline section below. |
| `verify_dataset` | Audits the dataset under `data/solves/` after a driver run — per-matchup stats, tier coverage, sample-record sanity check. |

## Performance model

Two reference machines:

| Machine | CPU | Cores / Threads | RAM | NUMA | ISA |
|---|---|---|---|---|---|
| Ryzen dev box (WSL2) | Ryzen 5950X @ ~4.5 GHz | 16 / 32 | 64 GB | 1 node | AVX2 |
| svl8 production server | 2× Xeon Gold 5220 @ 2.2 GHz | 36 / 72 | 503 GB | **2 nodes** | AVX-512 |

- Solver is **memory-bandwidth bound** *per solve*. Compiler flags move wall-clock by ~3%. Hot path is already hand-tuned to SIMD (9k+ AVX ops, 0 plain SSE in the release binary).
- **Single-NUMA-node concurrency does not help.** Rayon saturates all cores inside one `solve()`. Running two solves on the same socket just splits cores and contends on DRAM. (This is the only true statement on the Ryzen box.)
- **Cross-NUMA-node concurrency *does* help.** Each socket on svl8 has its own DRAM channels and L3 cache, so a `numactl --cpunodebind=N --membind=N` solve on node 0 doesn't fight a solve on node 1. **Two NUMA nodes ≈ two machines in one chassis** for this workload. Use `scripts/run_production.sh` — it auto-detects topology and launches one shard per node by default.
- **Per-matchup oversubscription.** 1 shard / NUMA is right for **SRP only** (8–17 GB working set, bandwidth-bound). For **4BP** (~130 MB, fits in 49.5 MiB L3 per socket → core-bound) you can run ~8 shards/node for big wall-clock wins. For **3BP** (~1.5 GB) ~4 shards/node is a good middle ground. See "[Cluster runs on svl8](#cluster-runs-on-svl8)" below.
- **Multi-machine sharding** is also wired (`SHARD_INDEX` / `SHARD_COUNT` + shared filesystem). ~10× cloud burst (e.g. spot c7a.16xlarge) cuts the full HU 200 BB dataset to ~3 hours for ~$50–100.
- `allocate_memory(true)` (compression) **slows** solves; it halves RAM at the cost of encode/decode per access. Only enable when a single spot won't fit in RAM. Driver gates this at 18 GB by default — appropriate for the 64 GB Ryzen box. On the 503 GB svl8 server, raise the threshold (e.g. `COMPRESS_THRESHOLD_GB=999`) so compression is never triggered. Current code path: `dataset_driver.rs:279` (constant `18.0` — to be made env-configurable).

## Throughput baseline (HU 200 BB, rich-flop config below)

| Matchup | per-spot | RAM | 1755 flops |
|---|---|---|---|
| SRP | ~140 s avg, 220 s worst | 8–17 GB | ~70 h |
| 3BP | ~12 s | 0.9–1.5 GB | ~6 h |
| 4BP | ~1.5 s | 0.05–0.13 GB | ~45 m |

Full dataset ≈ 3 days continuous on one machine. Tree-walker (feature extraction) cost is <1% of solve time — walking a 3BP tree (4,228 records) takes ~0.07 s; SRP projects to ~1–2 s/spot.

## Recommended TreeConfig (HU 200 BB dataset)

Rich flop, lean turn/river, 2% pot exploit target. A/B benchmark showed bet-tree complexity dominates exploitability target as a speed lever (~4× from going to 1 size on flop vs ~1.4× from 1%→2% exploit). The user prefers richer flop sizing because polar-vs-range-bet is the most teachable sizing decision for an LLM.

```rust
flop_bet_sizes:  ("33%,75%", "3x")
turn_bet_sizes:  ("75%",     "3x")
river_bet_sizes: ("75%",     "3x")
add_allin_threshold:   1.5
force_allin_threshold: 0.15
merging_threshold:     0.1
target_exploitability: 0.02 * pot
max_iter:              200
```

- Don't drop below 1 flop size — kills the small-range-bet vs polar decision, the most teachable sizing signal.
- Don't add a second turn size before a second river size — turn nodes appear many times via chance branching, so cost scales worse.
- 2% target is fine; LLM training averages over noise. A 0.5% re-solve on a chosen subset is a cheap follow-up.

## HU 200 BB ranges (worked examples)

These compile and solve. Note the descending-order discipline within each segment.

```rust
// BTN open (~84% — wide HU opener)
let btn_open = "22+,A2s+,K2s+,Q2s+,J4s+,T6s+,96s+,85s+,75s+,64s+,53s+,\
                A2o+,K5o+,Q8o+,J8o+,T8o+,97o+,87o,76o";

// BB call vs BTN 2.5x (~55% — wide HU defend)
let bb_call = "JJ-22,AQs-A2s,KJs-K2s,QJs-Q5s,J9s-J6s,T9s-T7s,96s+,85s+,75s+,64s+,\
               AJo-A2o,KJo-K8o,QJo-Q9o,JTo-J9o,T9o-T8o,98o,87o,76o,65o";

// BB 3-bet (~13%) — value + polar bluffs
let bb_3bet = "TT+,AQs+,AQo+,A5s,A4s,K9s,76s,65s";

// BTN call vs BB 3-bet (~22%)
let btn_call_vs_3bet = "99-22,AJs-A2s,KTs-K2s,QTs+,J9s+,T8s+,98s,87s,76s,65s,AJo,KQo";

// BTN 4-bet (~5%)
let btn_4bet = "QQ+,AKs,AKo,A5s";

// BB call vs 4-bet (~3.5%)
let bb_call_vs_4bet = "QQ-JJ,AKs,AKo";
```

Chip units: 1 chip = 0.01 BB → 200 BB = 20,000 chips. Pot/stack triples (chips):
- SRP: `starting_pot = 500`, `effective_stack = 19_750` (SPR ~40)
- 3BP: `starting_pot = 2_000`, `effective_stack = 19_000` (SPR ~9.5)
- 4BP: `starting_pot = 5_000`, `effective_stack = 17_500` (SPR ~3.5)

## Library API gotchas

- **Range strings must be descending within a segment.** `JJ-22` ✅, `22-JJ` panics. Same for `T9o-T8o` vs `T8o-T9o`. Fails at parse time with the message `"Range must be in descending order: ..."`.
- **`current_board()` returns sorted-ascending cards**, not the order specified. Treat the board as an unordered set.
- **`equity()` / `expected_values()` return `Vec<f32>`**, not `&[f32]`. Pass by reference when feeding helpers.
- **Always `cache_normalized_weights()` after navigating to a new node** before reading `equity()` or `normalized_weights()`, or you get stale data. Cheap — just call it at the top of any per-node computation.
- **`strategy()` is a flat `Vec<f32>` of length `n_combos * n_actions`**, indexed `combo + action * n_combos` (actions are the slow-changing dim).
- **Chance-node `available_actions()` returns isomorphism-grouped representatives**, not all 49/48 cards. To walk every dealt card, use `possible_cards()` (u64 bitmask) + `game.play(card as usize)`.
- **`apply_history()` is O(depth)** — it calls `back_to_root()` and replays. It's the only way to "go back up." Empirically still fast (walking a 3BP tree ≈ 0.07s).
- **`Action::Raise(amt)` is total chips, not increment.** `Bet(amt)` is the bet size in chips.
- **`is_terminal_node()` is true when the node has no children OR when the bet has reached the effective stack**, so don't assume "all-in" leaves are chance nodes.

## Tree-walker DFS pattern

The library has no `back_one()`; DFS pattern is recursive with `apply_history()` rewind. Pseudocode:

```rust
fn walk(game: &mut PostFlopGame, hist: &mut Vec<usize>, labels: &mut Vec<String>, out: &mut Vec<Record>) {
    if game.is_terminal_node() { return; }

    if game.is_chance_node() {
        let cards: Vec<u8> = (0u8..52).filter(|c| game.possible_cards() & (1u64 << c) != 0).collect();
        for &card in &sample(cards, n_chance_samples) {
            game.play(card as usize);
            hist.push(card as usize);  labels.push(format!("deal_{}", card_to_string(card).unwrap()));
            walk(game, hist, labels, out);
            labels.pop();  hist.pop();
            game.apply_history(hist);  // rewind
        }
    } else {
        out.push(build_record(game, labels));  // emit decision-node record
        for (a_idx, action) in game.available_actions().iter().enumerate() {
            game.play(a_idx);
            hist.push(a_idx);  labels.push(action_label(action));
            walk(game, hist, labels, out);
            labels.pop();  hist.pop();
            game.apply_history(hist);
        }
    }
}
```

See `examples/tree_walker_demo.rs` for the production version with feature extraction.

## Feature extraction pattern (for dataset records)

Per decision node, per player, compute from `game.equity(player)` × `game.normalized_weights(player)`:

1. `range_eq` — weighted mean equity
2. `nut` — % weight with eq > 0.85
3. `strong` / `marginal` / `weak` / `air` — buckets at 0.65 / 0.40 / 0.20
4. `histogram` — 10 equity bins
5. Symbolic flags: `range_advantage` ∈ {OOP, IP, EVEN} from `range_eq` diff (threshold 0.04); `nut_advantage` similarly (threshold 0.03)

Strategy label: `range_strategy: Vec<f32>` length `actions.len()`, weighted by the to-act player's normalized weights. This is the natural prediction target — the LLM may not condition on a specific hand at inference.

Reference impls: `examples/range_advantage_demo.rs` (root only), `examples/tree_walker_demo.rs` (full DFS).

## Canonical flop enumeration (1,755 classes)

For LLM training, solve once per strategically-distinct flop, not once per C(52,3) = 22,100 raw flop. Strategies are identical up to suit relabeling because preflop ranges in NLHE are suit-symmetric.

Breakdown:

| Rank shape | rank patterns | suit patterns | total |
|---|---|---|---|
| Three distinct ranks (e.g. K72) | C(13,3) = 286 | rainbow / monotone / two-tone (3 ways) = 5 | **1,430** |
| Pair + kicker (KK7) | 13 × 12 = 156 | kicker-suit-shares-pair / doesn't = 2 | **312** |
| Trips (KKK) | 13 | 1 | **13** |
| | | | **1,755** |

Canonicalize via min over the 24 suit permutations of the sorted 3-card vector:

```rust
fn canonical_flop(cards: [u8; 3]) -> [u8; 3] {
    let mut best: Option<[u8; 3]> = None;
    for perm in (0..4u8).permutations(4) {           // 24 suit relabelings
        let mut relabeled = cards.map(|c| (c / 4) * 4 + perm[(c % 4) as usize]);
        relabeled.sort_unstable();                    // canonical order within flop
        if best.map_or(true, |b| relabeled < b) { best = Some(relabeled); }
    }
    best.unwrap()
}
```

Iterate all 22,100 flops, canonicalize, dedupe into a `HashSet`, persist the 1,755 reps to `data/canonical_flops.json` once. Assert `len() == 1755`.

## Dataset pipeline (resumable driver)

End-to-end pipeline is wired up:

```
data/hu_200bb_ranges.txt   ─┐
data/canonical_flops*.txt  ─┤   src/canonical.rs (1755 flops + stratified order)
                            ├── examples/dataset_driver.rs
src/dataset_walker.rs       ─┘        │
                                       ▼
                            data/solves/<matchup>/<idx>_<flop>.jsonl
                                       │
                                       ▼
                            examples/verify_dataset.rs  (audit per-tier)
```

### Tiered runs (smoke → medium → full)

Use the `TIER` env var. Flops are processed in **stratified order** so any
prefix is maximally representative of the full 1,755:

```sh
# Smoke (100 flops × 3 matchups): ~5 hours total. Validates pipeline at scale.
TIER=smoke   cargo run --release --example dataset_driver

# Medium (500 flops): ~24 hours. Adds 400 new flops (smoke spots auto-skipped).
TIER=medium  cargo run --release --example dataset_driver

# Full (1,755 flops): ~85 hours total. Adds the remaining 1,255 flops.
TIER=full    cargo run --release --example dataset_driver

# Audit any tier after it completes
cargo run --release --example verify_dataset
```

Per-tier rough budget on this machine: 4BP ~ 3 s/spot, 3BP ~ 23 s, SRP ~ 145 s
(see [throughput baseline](#throughput-baseline-hu-200-bb-rich-flop-config-below)).
The first 100 stratified flops are guaranteed to cover every
(rank-shape × suit-pattern × high-card-bucket) bucket — verified by unit test.

### File layout & invariants

- Filenames use the **stable canonical sort index** (e.g. `1755_AcAdAh.jsonl`),
  not the stratified-order position. Re-running with a different `TIER` never
  causes file-name churn or recomputation — already-solved spots are skipped
  via file-presence check.
- Each solved spot writes `<idx>_<flop>.jsonl` + `<idx>_<flop>.meta`. The
  driver skips a spot only when **both** files exist.
- Atomic writes: `.tmp` file → fsync → rename. Killing the driver mid-spot
  cannot leave a half-written `.jsonl` visible.
- Shardable across machines: each worker runs with `FLOP_START=<offset>`
  `FLOP_LIMIT=<size>` covering its slice. File-presence is the only
  coordination needed (use a shared filesystem).

### Other useful driver env vars

```
MATCHUPS=SRP,3BP,4BP   subset (default: all three)
FLOP_LIMIT=N           override TIER size
FLOP_START=N           skip first N positions in stratified order
MAX_ITER=N             solver iteration cap (default: 200)
TARGET_PCT=0.02        exploitability target (default: 2% of pot)
TURN_SAMPLES=8         chance-sampling density for turn
RIVER_SAMPLES=6        chance-sampling density for river
OUT_DIR=path           override data/solves
COMBO_DATA=1           emit per-combo equity/weight/EV/strategy (see below)
```

### Combo-data output (`COMBO_DATA=1`, schema `combo-v2`)

When `COMBO_DATA=1` the driver writes `<idx>_<flop>.jsonl.zst` (zstd-compressed)
instead of `.jsonl`, and prefixes the file with a one-line header that embeds
everything a downstream consumer needs to decode per-combo arrays without a
Rust runtime. Approx ~20× smaller than the original dense format on
root-of-tree nodes, larger savings on deep nodes.

Build requires `--features zstd` (`run_production.sh` toggles this automatically
when `COMBO_DATA=1`).

Header (line 1):

```json
{"type":"header","schema":"combo-v2","matchup":"3BP","flop_idx":1,
 "flop":["2c","2d","2h"],"starting_pot":2000,"effective_stack":19000,
 "combos_oop":["AcAd","AcAh",...],
 "combos_ip" :["AcAd","AcAh",...]}
```

Node records (lines 2..N): unchanged range-aggregate fields plus

```json
"combo_data": {
  "oop": {"idx":[0,2,5,...], "eq":[0.510,...], "w":[0.012,...], "ev":[1850,...]},
  "ip" : {"idx":[1,4,...],   "eq":[...],       "w":[...],       "ev":[...]},
  "strategy": [0, [0.5,0.5,0], 2, ...]
}
```

- `oop`/`ip` are **sparse** — only combos with reach weight > 1e-6 appear.
  `idx` is the position in the header's `combos_oop`/`combos_ip`.
- `strategy` is aligned with the **player-to-act's** sparse idx list. Each entry is
  either an integer action index (pure, ≥99.5% of weight on one action) or a
  full distribution as floats.
- Quantization: equity & weight to 3 decimals, EV rounded to integer chips.
- The driver re-solves any spot whose `.meta` lacks `schema=combo-v2`, so
  switching `COMBO_DATA=1` on does a full re-run.

Reference Python decoder (no Rust, requires `pip install zstandard`):

```sh
python scripts/decode_combo_data.py path/to/0001_2c2d2h.jsonl.zst --densify --limit 1
```

### Generate input files (one-time setup after clone)

```sh
cargo run --release --example canonical_flops   # writes both order files to data/
cargo run --release --example verify_hu_ranges  # confirms the ranges template parses
```

## Sanity-check outputs (what working looks like)

Useful for verifying changes don't regress.

- `tree_walker_demo` on `SPOT=4bp` Kh7d2c: ~1.3 s solve, exploit ~0.83%, 1,448 records (16 flop + 184 turn + 1,248 river). At root, OOP checks 100% (capped 4BP range, IP has range eq ~64% / nut share ~27%). `range_advantage` and `nut_advantage` both "IP".
- `tree_walker_demo` on `SPOT=3bp` Kh7d2c: ~17 s solve, exploit ~0.97%, 4,228 records. At root, **OOP donk-bets 99%** (BB 3-bet range dominates on K-high — range eq 61.1%, nut share 19.6%). `range_advantage` and `nut_advantage` both "OOP".
- `range_advantage_demo`: Kh7d2c SRP root prints `range_advantage=IP nut_advantage=IP` with OOP checking 95.6%; Th9d8h prints `range_advantage=EVEN nut_advantage=EVEN` with OOP donk-betting 34%.
- `cargo test --release`: 38 unit + 1 (kuhn) + 2 (leduc) + 13 doc = 54 tests, all passing.

If `range_advantage` ever flips sign on a board that obviously favors one player, suspect either a stale `cache_normalized_weights()` call or a player-index mix-up (OOP=0, IP=1).

## Cluster runs on svl8

svl8 = 2-socket Xeon Gold 5220, 72 threads, 503 GB RAM, AVX-512, 2 NUMA nodes (node0: CPUs 0–17, 36–53; node1: 18–35, 54–71). L3 = 24.75 MiB / socket.

Launcher: `scripts/run_production.sh <tier> [matchups]`. It:
1. Builds `dataset_driver` in release mode (target-cpu=native picks up AVX-512 on this host).
2. Sanity-checks the binary for AVX-512 instructions via `objdump`.
3. Verifies `data/hu_200bb_ranges.txt` + `data/canonical_flops_stratified.txt` exist (generates the latter if missing).
4. Auto-detects NUMA topology, then runs matchups **sequentially in phases** (default order `4BP → 3BP → SRP`, fast→slow). Within each phase, launches `SHARDS_PER_NODE_<matchup> × NUMA_NODES` shards in parallel; each shard pinned via `numactl --physcpubind=<cpu-subset> --membind=<node>` to a disjoint CPU subset of its NUMA node. All shards in a phase stripe over the stratified flop list via `SHARD_INDEX` / `SHARD_COUNT`.
5. Logs per shard to `logs/<matchup>_node<N>_shard<S>.log`. Waits for each phase to finish before the next; after all phases, runs `verify_dataset`.

**Default per-matchup oversubscription** (override via env vars):

| Matchup | Shards / NUMA node | Threads / shard (on 36-cpu node) | Total parallel solves on 2 sockets | Rationale |
|---|---|---|---|---|
| 4BP | 8 (`SHARDS_PER_NODE_4BP`) | 4–5 | 16 | ~130 MB working set fits in 49.5 MiB L3 → core-bound |
| 3BP | 4 (`SHARDS_PER_NODE_3BP`) | 9 | 8 | ~1.5 GB / solve, mild bandwidth pressure |
| SRP | 1 (`SHARDS_PER_NODE_SRP`) | 36 | 2 | 8–17 GB / solve, DRAM-bandwidth bound; oversubscription doesn't help |

Recovery semantics: kill -9 on a shard is safe — atomic `.tmp → rename` writes mean half-written `.jsonl` files cannot exist. Restart with the same tier; file-presence check skips done spots. Each matchup phase is independent — if 4BP fails, the script logs and continues to 3BP.

**Known tuning gaps** (small, to address before/during the full run):
- `COMPRESS_THRESHOLD_GB` is a hardcoded 18.0 (`dataset_driver.rs:279`). On svl8's 503 GB RAM it should be raised to never trigger. Affects only the handful of SRP rainbow flops that cross the threshold.

## SLURM job arrays (Stanford SC cluster)

The dataset workload is embarrassingly parallel across stratified-order flops, atomic on write, and resumable via file-presence — so it maps cleanly onto a SLURM job array. The pattern:

- **Outer parallelism (across nodes):** `sbatch --array=0-(N-1)` launches N independent jobs. Each gets a disjoint contiguous slice of the 1755 stratified flops via `FLOP_START` / `FLOP_LIMIT`. No SLURM-side coordination needed.
- **Inner parallelism (within a node):** each array task runs `run_production.sh` on its allocated node, which itself does NUMA-aware per-matchup oversubscription (4BP=8 shards/node, 3BP=4, SRP=1).
- **Coordination:** shared filesystem (sailhome home dirs work, but a project-share is better for throughput) + atomic `.tmp → rename` writes + file-presence skip. Two tasks accidentally targeting overlapping flops cost extra wallclock but cannot corrupt output.
- **Preemption-safe:** `#SBATCH --requeue` plus the resume semantics means a preempted task restarts from the next un-done flop with seconds of wasted work.

Submit script: `scripts/slurm_array.sh` (fill in `--account`, `--partition`, walltime before first use).

### Resource request sizing

Per task (one node):

| Resource | Value | Why |
|---|---|---|
| `--cpus-per-task` | 36–72 (all of one node) | Solver uses every available thread within an NUMA-pinned shard. |
| `--mem` | 64 G minimum, 128 G recommended | SRP peak ~17 GB × 2 NUMA shards + OS/buffer; smaller fits if running 4BP-only. |
| `--gres=gpu` | **omit** | Workload is pure CPU. GPU partitions just waste your group's allocation. |
| `--time` | see table below | Walltime per task ≈ (slice_size × matchup_mix_time) ÷ shard_parallelism. |

Walltime per task ≈ how long it takes one node to chew through `WINDOW = ⌈1755 / array_size⌉` flops. On a Xeon Gold 5220-class node, single-shard per-spot times: 4BP ~3 s, 3BP ~23 s, SRP ~145 s. With per-matchup oversubscription, *effective* per-spot wall-clock on a 2-NUMA node:

| Matchup | Single-shard | × shards/node × NUMA | Effective per-spot |
|---|---|---|---|
| 4BP | 3 s | × 16 | ~0.2 s |
| 3BP | 23 s | × 8 | ~3 s |
| SRP | 145 s | × 2 | ~73 s |

So a slice of `W` flops × all 3 matchups ≈ `0.2W + 3W + 73W ≈ 76W` seconds, dominated by SRP. Sizing examples:

| `--array` size | Slice size | Walltime / task | Total wallclock if all nodes start together |
|---|---|---|---|
| 10 | ~176 flops | ~3.7 h | ~4 h |
| 20 | ~88 flops | ~1.9 h | ~2 h |
| 40 | ~44 flops | ~1 h | ~1 h |
| 80 | ~22 flops | ~30 min | ~30 min |

The right pick depends on (a) the partition's max concurrent jobs per user, and (b) how long jobs sit in queue vs run. **Recommended default: `--array=0-19%10`** (20 slices, 10 running at once) — usually within group quotas, and 10 nodes brings full-tier from ~85 h on one box to ~4 h.

### Single-matchup arrays (faster iteration)

For early-iteration runs on just 4BP / 3BP (so downstream LLM work can start while SRP is still solving), submit per-matchup:

```sh
# 4BP only — one node handles all 1755 in ~10 min, no array needed
sbatch --array=0-0 --time=00:30:00 --export=ALL,MATCHUPS=4BP scripts/slurm_array.sh

# 3BP only — 10 nodes × ~18 min each
sbatch --array=0-9 --time=01:00:00 --export=ALL,MATCHUPS=3BP scripts/slurm_array.sh

# SRP only — the big one; 20+ nodes
sbatch --array=0-19%20 --time=03:00:00 --export=ALL,MATCHUPS=SRP scripts/slurm_array.sh
```

### Output / log paths

- `logs/slurm_<jobid>_<taskid>.out` — SLURM stdout (the array task's view)
- `logs/<matchup>_node<N>_shard<S>.log` — per-shard driver stdout (inside `run_production.sh`)
- `data/solves/<matchup>/<idx>_<flop>.{jsonl,meta}` — actual dataset output

Put `data/solves/` on the fastest writable shared filesystem you have access to. Atomic writes are fsync-bounded, so a slow NFS hurts throughput more than you'd expect.

## Repository state

- **Branch:** `main`. All infrastructure committed.
- **Key commits (this work):**
  - `9d567d1` — `feat(dataset): HU 200BB stratified-tier generation infrastructure` (canonical-flop enum, stratified order, dataset driver, tree walker, ranges template, run/status scripts, CLAUDE.md notes). Adds 1755 canonical flops + tier constants (SMOKE=100, MEDIUM=500, FULL=1755).
  - `6f4e6e3` — earlier docs+examples for the same effort.
  - `d6666f9` — `.cargo/config.toml` native-CPU + fat-LTO release profile.
  - `2e9816c` — Rust 1.95 implicit-autoref fix in `action_tree.rs`.
- **Generated artifacts** (gitignored): `/data/solves/` (per-spot JSONL+meta), `/target.bak/`.
- **Dataset progress** under `data/solves/`:
  - 4BP: 100/100 smoke complete
  - 3BP: 2/100 smoke
  - SRP: 2/100 smoke
- **Examples added across the dataset work:** `btn_vs_bb_100bb`, `throughput_bench`, `hu_200bb_bench`, `srp_speedup_bench`, `range_advantage_demo`, `tree_walker_demo`, `canonical_flops`, `verify_hu_ranges`, `solve_srp_real_ranges`, `dataset_driver`, `verify_dataset`. All clean-build and run.
