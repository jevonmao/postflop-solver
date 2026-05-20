# Viewer iteration: research/analysis features

## Context

The Next.js viewer at `viewer/` currently exposes 3 routes (index, per-matchup
spot list, single-spot tree walk). It renders one node at a time with
aggregate range stats + range-weighted strategy. For research-paper /
dataset-analysis use it's missing:

1. **Per-combo visibility** — `combo_data` is in the schema and loaded but
   never displayed; the most teachable poker-research figure (a 13×13
   strategy/EV grid) cannot be produced.
2. **Dataset-level methods view** — exploitability, solve-time, memory, and
   coverage stats are scattered across `.meta` files with no aggregate view.
   This is exactly the material a methods section of a paper needs.
3. **Cross-spot aggregation** — no way to ask "across all K-high rainbow SRP
   flops, how often does OOP donk-bet at root?". Pattern discovery is the
   whole point of having 1755 canonical flops.

User picked these three for this iteration. Spot-comparison + CSV export was
deferred.

## Approach

Three additive features, each isolated to its own route + lib module. No
churn to existing `SpotViewer.tsx`, except adding a 13×13 panel that
gracefully hides when `combo_data` is absent.

---

### Feature 1 — 13×13 hand grid + combo heatmaps

**Solver-side prerequisite** (small Rust change):
`combo_data` in `src/dataset_walker.rs` currently emits parallel arrays
(`oop_equity`, `oop_weights`, `oop_ev`, `ip_*`, `strategy[combo][action]`)
but does NOT emit the `(Card, Card)` tuple for each index — without those
labels the viewer cannot map combo `i` to a grid cell. Fix:

- Extend `struct ComboData` (`src/dataset_walker.rs:29`) with
  `oop_combos: Vec<(Card, Card)>` and `ip_combos: Vec<(Card, Card)>`
  populated from `game.private_cards(0/1)` (already used elsewhere in the
  library — see `src/game/base.rs:249`).
- Extend `combo_data_json()` (`src/dataset_walker.rs:334`) to emit
  `"oop_combos":[["Ah","Kh"], …]` and `"ip_combos":[…]`. Use existing
  `card_to_string()` helper.
- Files: `src/dataset_walker.rs` only. No driver/CLI change needed.

After this change, regenerate a *small* combo-data sample so the viewer has
something to render:

```sh
COMBO_DATA=1 MATCHUPS=4BP FLOP_LIMIT=20 OUT_DIR=data/solves_combo \
  cargo run --release --example dataset_driver
```

4BP × 20 spots ≈ 1 minute on the Ryzen box. Land the heavier
combo-data re-solves later, separately — out of scope for this PR.

**Viewer-side rendering:**
- New `viewer/lib/handGrid.ts`: pure helper. Given `oop_combos` (or `ip_combos`)
  + a per-combo value array (weight × strategy[a], or EV, or equity), aggregate
  into a 13×13 matrix indexed by `(high_rank, low_rank)` where the diagonal is
  pocket pairs, upper triangle is suited, lower triangle is offsuit. Each cell
  is the weight-normalised average across the 4–12 specific combos in that
  bucket.
- New `viewer/components/HandGrid.tsx`: SVG 13×13 with cells colored by
  selected metric. Three view-mode toggles per side:
  `Strategy:<action>` (one cell-color = that action's frequency for that hand
  bucket), `Equity` (range-eq per bucket), `EV` (cash-EV per bucket, normalised
  by min/max in view). Show numeric % overlay on hover (title attr) plus a
  small legend.
- Integrate into `SpotViewer.tsx`: render `<HandGrid side="OOP" …/>` and
  `<HandGrid side="IP" …/>` panels below the existing range bars, only when
  `node.combo_data` is present. Otherwise show a small hint
  "(re-solve with `COMBO_DATA=1` to see per-combo grid)".

Files touched: `src/dataset_walker.rs`, `viewer/lib/handGrid.ts` (new),
`viewer/components/HandGrid.tsx` (new), `viewer/components/SpotViewer.tsx`,
`viewer/lib/types.ts` (add `oop_combos`/`ip_combos` to `ComboData`).

---

### Feature 2 — Dataset overview / methods dashboard

New route `viewer/app/dataset/page.tsx`. Server component; reads all `.meta`
files via existing `lib/data.ts` listing logic, computes aggregates in-process,
renders static charts as inline SVG (no chart library — keep `package.json`
unchanged).

Panels:

1. **Solve-quality table** — per matchup: count solved, mean / median / p90
   exploitability % of pot, mean / median / p90 solve_s, mean / max memory_gb,
   total CPU-hours spent. Direct paper-methods material.
2. **Exploitability distribution** — small SVG histogram per matchup
   (10 bins, x = exploit % of pot).
3. **Solve-time distribution** — small SVG histogram per matchup.
4. **Coverage by board texture** — bar chart of solved counts split by
   `{three-distinct, paired, trips} × {rainbow, two-tone, monotone}` (see
   Feature 3 for the shared classifier).
5. **Config snapshot** — first spot's `target_pct`, `max_iter`,
   `turn_samples`, `river_samples` (constant across the run).

New files: `viewer/app/dataset/page.tsx`, `viewer/lib/aggregate.ts` (pure
helpers: `summarizeMetas`, `boardTextureCounts`). Add a link in the index
`/` page header.

---

### Feature 3 — Cross-spot aggregation explorer

New route `viewer/app/aggregate/page.tsx` (server component, query-param
driven). Lets a researcher pose: "filter spots by texture/matchup/high-card,
group by X, aggregate Y at the *root* decision node".

Implementation:

- New `viewer/lib/boardTexture.ts`: pure classifier. Input: 3-card board
  string (e.g. `"Kh7d2c"`). Outputs:
  - `rankShape`: `three_distinct | paired | trips`
  - `suitPattern`: `rainbow | two_tone | monotone`
  - `highCardBucket`: `A | K | Q | J | T | 9-7 | 6-2`
  - `connectivity`: `connected | one_gap | disconnected` (based on the two
    highest ranks)
- Server-side: load all `<idx>_<flop>.jsonl` for selected matchups, take only
  the root record (the one with `history: []`), tag with texture features,
  group/aggregate per the URL params.
- UI: simple controls (`<select>` × 4: matchup, group_by, agg_metric, filter)
  via plain `<form method="GET">` — Next.js server components re-render on
  submit. No client state.
- Metrics: `oop.range_eq`, `ip.range_eq`, `oop.nut`, `ip.nut`,
  `range_strategy[0]` (check freq), `range_strategy[any bet]` (aggregated
  bet-freq), exploit % of pot.
- Output: a table (group → n_spots, mean, std, min, max) + a tiny SVG bar
  chart of the mean.
- Cache: this is the only feature that reads *every* JSONL file. For the full
  1755-flop dataset that's heavy. Strategy: only read the *first* JSONL line
  of each file (the root record is always first, since `walk()` emits in
  pre-order DFS), so per-file IO is O(one line). Implement as a streaming
  read in `lib/aggregate.ts` (Node `readline` against a single-line need).

New files: `viewer/app/aggregate/page.tsx`, `viewer/lib/boardTexture.ts`,
extend `viewer/lib/aggregate.ts` from Feature 2 with the root-only reader.

Link to it from index `/`.

---

## Files to modify / create

Modify:
- `src/dataset_walker.rs` — add combo-label fields + JSON emission
- `viewer/lib/types.ts` — `ComboData.oop_combos`, `ComboData.ip_combos`
- `viewer/components/SpotViewer.tsx` — mount `<HandGrid>` panels
- `viewer/app/page.tsx` — add nav links to `/dataset` and `/aggregate`

Create:
- `viewer/lib/handGrid.ts`, `viewer/components/HandGrid.tsx`
- `viewer/lib/aggregate.ts`, `viewer/lib/boardTexture.ts`
- `viewer/app/dataset/page.tsx`, `viewer/app/aggregate/page.tsx`

Out of scope (per user selection): spot-comparison view, CSV/JSON export
button.

---

## Verification

1. `cargo build --release` after `dataset_walker.rs` change; run
   `COMBO_DATA=1 MATCHUPS=4BP FLOP_LIMIT=5 OUT_DIR=data/solves_combo
   cargo run --release --example dataset_driver`. Confirm a sample JSONL
   contains `"oop_combos":[["…","…"], …]` with length == `oop_equity.length`.
2. `cd viewer && npm run dev`. Smoke each route:
   - `/` — still works, has new nav links.
   - `/spots/4BP/0001_2c2d2h` — existing behaviour unchanged; grid panels
     show "(re-solve with COMBO_DATA=1)" hint when combo data missing.
   - After pointing `DATA_DIR=…/solves_combo`, visit a combo-enabled spot and
     verify both 13×13 grids render with sane colors (e.g. premium hands
     bet-heavy in OOP 4BP).
3. `/dataset` — exploitability + solve-time histograms match values from
   `verify_dataset` example.
4. `/aggregate?matchup=4BP&group_by=suit_pattern&metric=oop_range_eq` —
   rainbow ≈ two_tone ≈ monotone within a few percent on the smoke set;
   group counts add up to total root records.
5. Type-check: `npm run build` from `viewer/` succeeds.

No tests added — viewer has none; following project convention.
