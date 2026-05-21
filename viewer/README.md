# Postflop Viewer

Next.js webviewer for previewing the solved HU 200BB postflop dataset.
Supports all three matchups (SRP / 3BP / 4BP); the index page reflects
whichever matchups currently have solved spots on disk.

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
