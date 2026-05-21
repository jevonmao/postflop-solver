# Postflop Viewer

Next.js webviewer for previewing the solved HU 200BB postflop dataset.
Supports all three matchups (SRP / 3BP / 4BP); the index page reflects
<<<<<<< HEAD
whichever matchups currently have solved spots on disk. Reads both the plain
dense `<stem>.jsonl` format and the zstd-compressed `combo-v2`
`<stem>.jsonl.zst` format.

## Run

```sh
cd viewer
npm install
npm run dev
# open http://localhost:3000
```

## Where it reads from

The viewer reads the directory that **directly contains the matchup folders**
(`<dir>/4BP/0022_2c2dKc.jsonl`, etc.). Resolution order:

1. `SOLVES_DIR` env var — explicit path, used as-is (recommended).
2. `DATA_DIR` env var — legacy; the viewer appends `solves/`.
3. Default — first existing of `../solves_combos`, then `../data/solves`
   (relative to the viewer cwd).

```sh
SOLVES_DIR=/absolute/path/to/solves_combos npm run dev
```

Persist via `viewer/.env.local` (gitignored):

```
SOLVES_DIR=/absolute/path/to/solves_combos
```

The index page prints the resolved path under the heading so you can confirm
which directory is in effect. If no matchup folders are found there it also
shows an amber hint reminding you to set `SOLVES_DIR`.

For the full combo-v2 dataset in `solves-combo-full/`, use the `.viewer-data-combo/`
symlink root in the repo (see `.viewer-data-combo/` in the repo root) or set
`SOLVES_DIR` directly. Reading `.jsonl.zst` files requires the `zstd` CLI on
`PATH`; on big SRP spots, run the dev server with
`NODE_OPTIONS=--max-old-space-size=8192`.

## What you get

- **Index (`/`)** — one card per matchup with solved / 1755 progress.
- **Spots list (`/spots/<matchup>`)** — clickable grid of solved flops with
  exploitability, solve time, and combo-data flag.
- **Spot detail (`/spots/<matchup>/<idx_flop>`)** — interactive tree walk
  starting at the flop root. Click an action to descend; click a turn / river
  card at chance nodes to follow that branch; click any breadcrumb step to
  rewind. Each decision node shows board / pot / SPR / to-act, both ranges'
  equity bucket bars + 10-bin equity histograms, range and nut advantage
  badges, and the range-weighted strategy with one bar per action.
- **Per-combo 13×13 grids** — below the strategy panel, every decision node
  with combo-v2 `combo_data` renders an OOP and an IP hand grid. Toggle each
  grid between **range** (share of range per hand bucket), **equity**,
  **EV** (chips), and one mode **per action** showing that action's
  frequency. Cells are reach-weighted averages over the 4–12 specific combos
  in each bucket; hover any cell for the exact breakdown. The strategy modes
  appear only on the grid for the side that is to act.

## Architecture notes

- Files are read at request time (`force-dynamic` on pages), so re-running the
  solver shows up immediately on refresh — no rebuild needed.
- A combo-v2 SRP spot decompresses to ~800 MB, almost all of it `combo_data`,
  so the whole tree's per-combo data cannot be shipped to the browser. Instead:
  - the page ships **aggregate-only** records and builds the navigation trie
    client-side (`SpotViewer.tsx`);
  - each decision node fetches **just its own** combo data on demand from
    `GET /api/combo/<matchup>/<stem>?h=<json history>`;
  - `lib/data.ts` decompresses + parses a spot once and holds the full result
    (with combo data) in a small server-side LRU, so the first node view of a
    spot is slow (~decompress time) and the rest are instant.
- Grid aggregation is pure (`lib/handGrid.ts`); rendering is `HandGrid.tsx`.
