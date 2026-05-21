# Limped-pot single-flop benchmark (HU 200 BB)

**Date:** 2026-05-20
**Machine:** Ryzen 5950X dev box (WSL2), 16C/32T, 64 GB RAM, AVX2
**Example:** `examples/limp_pot_bench.rs` — `cargo run --release --example limp_pot_bench`

## Question

Can the solver handle a HU **limped pot**, and what does it cost relative to
SRP/3BP/4BP? A limped pot = SB completes the small blind, BB checks option.

## Spot definition

| Field | Value |
|---|---|
| OOP range | `BB_VS_SB_LIMP / CALL` (BB checks option) — 1302 combos |
| IP range | `SB_FIRST_ACTION / CALL` (SB limps) — 1290 combos |
| `starting_pot` | 200 chips (2.00 BB) |
| `effective_stack` | 19,800 chips (198 BB behind) |
| **SPR** | **~99** — the highest of any HU 200 BB spot |
| TreeConfig | production: flop `33%,75%`, turn/river `75%`, `3x`; 2% pot target; `max_iter=200` |
| Flop | `Kh7d2c` |

## Results

| Metric | Value |
|---|---|
| Build time | 0.61 s |
| Memory (uncompressed) | **46.05 GB** |
| Memory (compressed) | **23.20 GB** |
| Compression | **forced ON** (uncompressed > 18 GB driver threshold) |
| Solve time | **253.06 s** |
| Converged at | iteration 90 / 200 |
| Final exploitability | 3.95 chips = **1.97% of pot** (hit 2% target) |

## Comparison to existing matchups

| Matchup | SPR | Memory (uncompressed) | Per-spot solve |
|---|---|---|---|
| 4BP | ~3.5 | 0.05–0.13 GB | ~1.5 s |
| 3BP | ~9.5 | 0.9–1.5 GB | ~12 s |
| SRP | ~40 | 8–17 GB | ~140 s |
| **LIMP** | **~99** | **~46 GB** | **~253 s** |

The limped pot is, as expected, **the heaviest HU spot** — ~3× the memory
footprint of the worst SRP flop and ~1.8× the wall-clock. The high SPR (~99)
means a deep multi-street bet tree with much more room to maneuver.

## Interpretation & recommendations

- **It works.** The solver converges cleanly to the 2% target — no structural
  barrier to limped pots. The ranges are already present in
  `data/hu_200bb_ranges.txt` (`SB_FIRST_ACTION/CALL`, `BB_VS_SB_LIMP/CALL`).
- **Memory is the constraint.** 46 GB uncompressed does *not* fit alongside the
  OS on the 64 GB Ryzen box, so the 18 GB driver threshold correctly forces
  compression. The 253 s figure **includes compression encode/decode overhead** —
  an uncompressed solve would be faster per iteration but needs ~46 GB free.
- **svl8 (503 GB RAM)** can run this uncompressed. With `COMPRESS_THRESHOLD_GB`
  raised, expect a faster per-spot time than 253 s. Like SRP, it is
  bandwidth-bound at this size → **1 shard / NUMA node**, no oversubscription.
- **Throughput projection (single shard):** ~253 s × 1755 ≈ **123 h** for the
  full canonical-flop set. On svl8's 2 NUMA nodes that halves to ~62 h; cloud
  burst sharding scales it down further. This roughly **doubles total dataset
  cost** if added as a 4th matchup (SRP currently dominates at ~70 h).

### To wire LIMP into the dataset driver

Add to `MATCHUPS` in `examples/dataset_driver.rs`:

```rust
Matchup {
    label: "LIMP",
    oop_spot: "BB_VS_SB_LIMP",   oop_action: RangeAction::Call,
    ip_spot:  "SB_FIRST_ACTION", ip_action:  RangeAction::Call,
    starting_pot: 200, effective_stack: 19_800,
},
```

Ranges already exist; no template edit needed. Before a full run, raise
`COMPRESS_THRESHOLD_GB` on svl8 so the 46 GB working set stays uncompressed.
