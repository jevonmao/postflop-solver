# Postflop Viewer

Next.js webviewer for previewing the solved HU 200BB postflop dataset at
`../data/solves/`. Supports all three matchups (SRP / 3BP / 4BP); the index
page reflects whichever matchups currently have solved spots on disk.

## Run

```sh
cd viewer
npm install
npm run dev
# open http://localhost:3000
```

By default it reads `../data/solves/` (relative to `viewer/`). Override with:

```sh
DATA_DIR=/path/to/repo/data npm run dev
```

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

Per-combo data (when `combo_data=true`) is loaded but not yet rendered.

## Notes

- Files are read at request time (`force-dynamic` on pages), so re-running the
  solver shows up immediately on refresh — no rebuild needed.
- All UI fits in a single client component (`SpotViewer.tsx`). The trie is
  built once per spot from the flat JSONL records and navigation is
  in-memory.
