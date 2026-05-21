import type { SideCombo } from './types';

// Rank index 0..12 maps to 2..A.
const RANKS = '23456789TJQKA';
// Display order for the 13×13 grid: row/col 0 = A, row/col 12 = 2.
export const GRID_RANKS = 'AKQJT98765432'.split('');

export interface ParsedCombo {
  hi: number;     // higher rank index (0..12)
  lo: number;     // lower rank index
  suited: boolean;
  pair: boolean;
}

/** Parse a 4-char combo string like "AcKd" / "7h7s". Returns null if malformed. */
export function parseCombo(s: string): ParsedCombo | null {
  if (!s || s.length !== 4) return null;
  const r1 = RANKS.indexOf(s[0]);
  const r2 = RANKS.indexOf(s[2]);
  if (r1 < 0 || r2 < 0) return null;
  return {
    hi: Math.max(r1, r2),
    lo: Math.min(r1, r2),
    suited: s[1] === s[3],
    pair: r1 === r2,
  };
}

/** Canonical hand-class label, e.g. "AA", "AKs", "T9o". */
export function classLabel(p: ParsedCombo): string {
  if (p.pair) return RANKS[p.hi] + RANKS[p.hi];
  return RANKS[p.hi] + RANKS[p.lo] + (p.suited ? 's' : 'o');
}

/** Grid cell (row, col) for a combo. Diagonal = pairs, upper-right = suited,
 *  lower-left = offsuit — the conventional poker hand grid. */
export function cellPos(p: ParsedCombo): { row: number; col: number } {
  const hiG = 12 - p.hi; // A -> 0
  const loG = 12 - p.lo;
  if (p.pair) return { row: hiG, col: hiG };
  if (p.suited) return { row: hiG, col: loG };  // upper-right (row < col)
  return { row: loG, col: hiG };                // lower-left  (row > col)
}

export interface GridCell {
  /** Sum of reach weight of every specific combo in this bucket. */
  weight: number;
  /** Reach-weighted mean of the selected metric across the bucket's combos. */
  value: number;
  /** Number of specific combos that landed in the bucket. */
  count: number;
  label: string;  // "AKs"
}

export type Grid = GridCell[][];

function emptyGrid(): Grid {
  return Array.from({ length: 13 }, (_, r) =>
    Array.from({ length: 13 }, (_, c) => ({
      weight: 0, value: 0, count: 0,
      label: cellLabel(r, c),
    })),
  );
}

function cellLabel(row: number, col: number): string {
  const hi = GRID_RANKS[Math.min(row, col)];
  const lo = GRID_RANKS[Math.max(row, col)];
  if (row === col) return hi + hi;
  return row < col ? `${hi}${lo}s` : `${hi}${lo}o`;
}

/**
 * Aggregate a sparse per-combo array into a 13×13 grid. `metric(p)` returns the
 * value for sparse position `p`; cells hold the reach-weighted mean of it.
 */
export function buildGrid(
  labels: string[],
  combo: SideCombo,
  metric: (p: number) => number,
): Grid {
  const grid = emptyGrid();
  const sumWV = Array.from({ length: 13 }, () => new Array(13).fill(0));
  for (let p = 0; p < combo.idx.length; p++) {
    const label = labels[combo.idx[p]];
    const pc = parseCombo(label);
    if (!pc) continue;
    const { row, col } = cellPos(pc);
    const w = combo.w[p] ?? 0;
    const cell = grid[row][col];
    cell.weight += w;
    cell.count += 1;
    sumWV[row][col] += w * metric(p);
  }
  for (let r = 0; r < 13; r++) {
    for (let c = 0; c < 13; c++) {
      const cell = grid[r][c];
      cell.value = cell.weight > 1e-9 ? sumWV[r][c] / cell.weight : 0;
    }
  }
  return grid;
}

/** Action frequency for a to-act combo at sparse position `p`. */
export function strategyFreq(
  strategy: Array<number | number[]>, p: number, action: number,
): number {
  const e = strategy[p];
  if (e === undefined) return 0;
  if (typeof e === 'number') return e === action ? 1 : 0;
  return e[action] ?? 0;
}

/** Total reach weight across a grid (for normalising the range view). */
export function totalWeight(grid: Grid): number {
  let t = 0;
  for (const row of grid) for (const cell of row) t += cell.weight;
  return t;
}

/** Max reach weight of any single cell (for colour-scaling the range view). */
export function maxWeight(grid: Grid): number {
  let m = 0;
  for (const row of grid) for (const cell of row) m = Math.max(m, cell.weight);
  return m;
}
