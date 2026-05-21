'use client';

import { useMemo, useState } from 'react';
import type { SideCombo } from '@/lib/types';
import {
  buildGrid, strategyFreq, totalWeight, maxWeight,
  type Grid, type GridCell,
} from '@/lib/handGrid';

interface Props {
  side: 'OOP' | 'IP';
  labels: string[];                          // header combos_oop / combos_ip
  combo: SideCombo;
  actions: string[];
  /** Present only when this side is the one to act at this node. */
  strategy?: Array<number | number[]>;
}

type Mode =
  | { kind: 'range' }
  | { kind: 'equity' }
  | { kind: 'ev' }
  | { kind: 'strategy'; action: number };

const SIDE_RGB: Record<'OOP' | 'IP', [number, number, number]> = {
  OOP: [37, 99, 235],   // blue
  IP:  [220, 38, 38],   // red
};

export function HandGrid({ side, labels, combo, actions, strategy }: Props) {
  const [mode, setMode] = useState<Mode>({ kind: 'range' });

  const grid = useMemo<Grid>(() => {
    switch (mode.kind) {
      case 'range':
        return buildGrid(labels, combo, () => 1);
      case 'equity':
        return buildGrid(labels, combo, (p) => combo.eq[p] ?? 0);
      case 'ev':
        return buildGrid(labels, combo, (p) => combo.ev[p] ?? 0);
      case 'strategy':
        return buildGrid(labels, combo, (p) =>
          strategy ? strategyFreq(strategy, p, mode.action) : 0);
    }
  }, [mode, labels, combo, strategy]);

  const total = useMemo(() => totalWeight(grid), [grid]);
  const maxW  = useMemo(() => maxWeight(grid), [grid]);
  const evRange = useMemo(() => {
    let lo = Infinity, hi = -Infinity;
    for (const row of grid) for (const c of row) {
      if (c.weight <= 1e-9) continue;
      lo = Math.min(lo, c.value); hi = Math.max(hi, c.value);
    }
    return { lo: isFinite(lo) ? lo : 0, hi: isFinite(hi) ? hi : 1 };
  }, [grid]);

  const rgb = SIDE_RGB[side];

  function cellStyle(c: GridCell): React.CSSProperties {
    if (c.weight <= 1e-9) return { background: '#0a0a0a' };
    if (mode.kind === 'range') {
      const t = maxW > 0 ? c.weight / maxW : 0;
      return { background: `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${0.12 + 0.88 * t})` };
    }
    if (mode.kind === 'equity') {
      const v = Math.max(0, Math.min(1, c.value));
      return { background: `hsl(${v * 125}, 60%, ${24 + v * 14}%)` };
    }
    if (mode.kind === 'ev') {
      const span = evRange.hi - evRange.lo || 1;
      const t = (c.value - evRange.lo) / span;
      return { background: `hsl(205, 70%, ${16 + t * 46}%)` };
    }
    // strategy
    const v = Math.max(0, Math.min(1, c.value));
    return { background: `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${0.06 + 0.94 * v})` };
  }

  function cellText(c: GridCell): string {
    if (c.weight <= 1e-9) return '';
    switch (mode.kind) {
      case 'range':    return `${((c.weight / (total || 1)) * 100).toFixed(1)}`;
      case 'equity':   return `${(c.value * 100).toFixed(0)}`;
      case 'ev':       return `${Math.round(c.value)}`;
      case 'strategy': return `${(c.value * 100).toFixed(0)}`;
    }
  }

  function cellTitle(c: GridCell): string {
    if (c.weight <= 1e-9) return `${c.label} — not in range`;
    const head = `${c.label} · ${c.count} combo${c.count === 1 ? '' : 's'}`;
    switch (mode.kind) {
      case 'range':
        return `${head} · ${((c.weight / (total || 1)) * 100).toFixed(2)}% of range`;
      case 'equity':
        return `${head} · equity ${(c.value * 100).toFixed(1)}%`;
      case 'ev':
        return `${head} · EV ${Math.round(c.value)} chips`;
      case 'strategy':
        return `${head} · ${actions[mode.action]} ${(c.value * 100).toFixed(1)}%`;
    }
  }

  const dotCls = side === 'OOP' ? 'bg-oop' : 'bg-ip';

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={`inline-block w-2 h-2 rounded-full ${dotCls}`} />
          <span className="font-semibold text-sm">{side} hand grid</span>
        </div>
        <span className="text-[10px] text-neutral-500">{combo.idx.length} combos</span>
      </div>

      <div className="flex flex-wrap gap-1 mb-2">
        <ModeBtn on={mode.kind === 'range'}  onClick={() => setMode({ kind: 'range' })}>range</ModeBtn>
        <ModeBtn on={mode.kind === 'equity'} onClick={() => setMode({ kind: 'equity' })}>equity</ModeBtn>
        <ModeBtn on={mode.kind === 'ev'}     onClick={() => setMode({ kind: 'ev' })}>EV</ModeBtn>
        {strategy && actions.map((a, i) => (
          <ModeBtn
            key={a}
            on={mode.kind === 'strategy' && mode.action === i}
            onClick={() => setMode({ kind: 'strategy', action: i })}
          >{a}</ModeBtn>
        ))}
      </div>

      <div className="select-none">
        {grid.map((row, r) => (
          <div key={r} className="flex">
            {row.map((cell, c) => (
              <div
                key={c}
                title={cellTitle(cell)}
                style={cellStyle(cell)}
                className="relative flex-1 aspect-square flex flex-col items-center justify-center
                           border border-neutral-950/60 min-w-0"
              >
                <span className="text-[7px] leading-none text-neutral-300/70 font-mono">
                  {cell.label}
                </span>
                <span className="text-[8px] leading-tight font-semibold tabular-nums text-neutral-50">
                  {cellText(cell)}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>

      <Legend mode={mode} evRange={evRange} side={side} />
    </div>
  );
}

function ModeBtn({
  on, onClick, children,
}: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 rounded text-[10px] font-mono border transition ${
        on
          ? 'border-neutral-500 bg-neutral-700 text-neutral-50'
          : 'border-neutral-800 bg-neutral-950 text-neutral-400 hover:border-neutral-600'
      }`}
    >{children}</button>
  );
}

function Legend({
  mode, evRange, side,
}: { mode: Mode; evRange: { lo: number; hi: number }; side: 'OOP' | 'IP' }) {
  let text = '';
  if (mode.kind === 'range')    text = 'cell = % of range · shade ∝ bucket weight';
  if (mode.kind === 'equity')   text = 'cell = equity % · red low → green high';
  if (mode.kind === 'ev')       text = `cell = EV chips · range ${Math.round(evRange.lo)}–${Math.round(evRange.hi)}`;
  if (mode.kind === 'strategy') text = `cell = action freq % · darker ${side} = more often`;
  return <div className="mt-2 text-[10px] text-neutral-500">{text}</div>;
}
