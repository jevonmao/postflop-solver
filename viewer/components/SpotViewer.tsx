'use client';

import { useEffect, useMemo, useState } from 'react';
import type { NodeRecord, RangeStats, SpotMeta, NodeCombo } from '@/lib/types';
import { buildTrie, isChanceNode, navigate, type TrieNode } from '@/lib/tree';
import { Board, Card } from './Card';
import { HandGrid } from './HandGrid';

interface Props {
  matchup: string;
  stem: string;
  records: NodeRecord[];
  meta?: SpotMeta;
}

export function SpotViewer({ matchup, stem, records, meta }: Props) {
  const trie = useMemo(() => buildTrie(records), [records]);
  const [history, setHistory] = useState<string[]>([]);

  const current = useMemo(() => navigate(trie, history), [trie, history]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-6">
      <aside className="space-y-4">
        <MetaCard matchup={matchup} stem={stem} meta={meta} />
        <HistoryBreadcrumb history={history} onJump={(i) => setHistory(history.slice(0, i))} />
      </aside>

      <section className="space-y-4">
        {!current && <Empty message="Path not found in this dataset." />}
        {current && current.record && (
          <DecisionView
            node={current}
            matchup={matchup}
            stem={stem}
            onDescend={(k) => setHistory([...history, k])}
          />
        )}
        {current && !current.record && isChanceNode(current) && (
          <ChancePicker
            node={current}
            onPick={(k) => setHistory([...history, k])}
          />
        )}
        {current && !current.record && !isChanceNode(current) && current.children.size === 0 && (
          <Empty message="Terminal node — game ends here (fold or all-in showdown). No record emitted." />
        )}
      </section>
    </div>
  );
}

function MetaCard({ matchup, stem, meta }: { matchup: string; stem: string; meta?: SpotMeta }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-sm">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-lg font-semibold">{matchup}</span>
        <span className="text-xs text-neutral-500 font-mono">{stem}</span>
      </div>
      {meta && (
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-neutral-400">
          <Cell k="exploit"   v={`${meta.exploitability_pct_pot.toFixed(2)}%`} />
          <Cell k="records"   v={meta.n_records.toLocaleString()} />
          <Cell k="solve"     v={`${meta.solve_s.toFixed(1)}s`} />
          <Cell k="walk"      v={`${meta.walk_s.toFixed(1)}s`} />
          <Cell k="memory"    v={`${meta.memory_gb.toFixed(2)} GB`} />
          <Cell k="iter"      v={`${meta.max_iter}`} />
          <Cell k="turn smp"  v={`${meta.turn_samples}`} />
          <Cell k="river smp" v={`${meta.river_samples}`} />
          <Cell k="combo"     v={meta.combo_data ? 'yes' : 'no'} />
          <Cell k="zstd"      v={meta.compressed ? 'yes' : 'no'} />
        </dl>
      )}
    </div>
  );
}
const Cell = ({ k, v }: { k: string; v: string }) => (
  <>
    <dt className="text-neutral-600">{k}</dt>
    <dd className="text-neutral-200 text-right tabular-nums">{v}</dd>
  </>
);

function HistoryBreadcrumb({ history, onJump }: { history: string[]; onJump: (i: number) => void }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-xs space-y-1">
      <div className="text-neutral-500 mb-1">History</div>
      <button
        onClick={() => onJump(0)}
        className="block w-full text-left px-2 py-1 rounded hover:bg-neutral-800 font-mono"
      >
        <span className="text-neutral-500">root</span>
      </button>
      {history.map((step, i) => (
        <button
          key={i}
          onClick={() => onJump(i + 1)}
          className="block w-full text-left px-2 py-1 rounded hover:bg-neutral-800 font-mono text-neutral-200"
        >
          {'  '.repeat(i)}└ {step}
        </button>
      ))}
    </div>
  );
}

function DecisionView({
  node,
  matchup,
  stem,
  onDescend,
}: { node: TrieNode; matchup: string; stem: string; onDescend: (key: string) => void }) {
  const r = node.record!;
  return (
    <>
      <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <Board cards={r.board} size="lg" />
          <div className="text-right text-xs text-neutral-500 space-y-1">
            <div>pot <span className="text-neutral-200 font-mono">{r.pot.toLocaleString()}</span></div>
            <div>stack <span className="text-neutral-200 font-mono">{r.eff_stack.toLocaleString()}</span></div>
            <div>SPR <span className="text-neutral-200 font-mono">{r.spr.toFixed(2)}</span></div>
          </div>
        </div>
        <div className="text-sm">
          <span className="text-neutral-500">to act: </span>
          <span className={r.to_act === 'O' ? 'text-oop font-semibold' : 'text-ip font-semibold'}>
            {r.to_act === 'O' ? 'OOP (BB)' : 'IP (BTN)'}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <RangePanel label="OOP" stats={r.oop} color="oop" />
        <RangePanel label="IP"  stats={r.ip}  color="ip"  />
      </div>

      <AdvantageBadges record={r} />

      <StrategyPanel record={r} onDescend={onDescend} node={node} />

      <ComboGrids matchup={matchup} stem={stem} record={r} />
    </>
  );
}

function ComboGrids({
  matchup, stem, record,
}: { matchup: string; stem: string; record: NodeRecord }) {
  const [state, setState] = useState<
    { kind: 'loading' } | { kind: 'none' } | { kind: 'error'; msg: string } | { kind: 'ok'; data: NodeCombo }
  >({ kind: 'loading' });

  const histKey = record.history.join(' ');

  useEffect(() => {
    let cancelled = false;
    setState({ kind: 'loading' });
    const h = encodeURIComponent(JSON.stringify(record.history));
    fetch(`/api/combo/${matchup}/${stem}?h=${h}`)
      .then(async (res) => {
        if (cancelled) return;
        if (res.status === 404) { setState({ kind: 'none' }); return; }
        if (!res.ok) { setState({ kind: 'error', msg: `HTTP ${res.status}` }); return; }
        const data = (await res.json()) as NodeCombo;
        if (!cancelled) setState({ kind: 'ok', data });
      })
      .catch((e) => { if (!cancelled) setState({ kind: 'error', msg: String(e) }); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchup, stem, histKey]);

  if (state.kind === 'loading') {
    return (
      <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-xs text-neutral-500">
        Loading per-combo data…
      </div>
    );
  }
  if (state.kind === 'none') {
    return (
      <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-xs text-neutral-600 italic">
        No per-combo data for this node — re-solve with <code className="text-neutral-400">COMBO_DATA=1</code> to populate 13×13 grids.
      </div>
    );
  }
  if (state.kind === 'error') {
    return (
      <div className="rounded-lg border border-red-900 bg-neutral-900 p-4 text-xs text-red-400">
        Failed to load combo data: {state.msg}
      </div>
    );
  }

  const { data } = state;
  const cd = data.combo_data;
  return (
    <div>
      <h3 className="text-sm font-semibold mb-2">
        Per-combo grids
        <span className="text-neutral-500 font-normal"> — equity · range · EV · strategy</span>
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <HandGrid
          side="OOP"
          labels={data.combos_oop}
          combo={cd.oop}
          actions={data.actions}
          strategy={data.to_act === 'O' ? cd.strategy : undefined}
        />
        <HandGrid
          side="IP"
          labels={data.combos_ip}
          combo={cd.ip}
          actions={data.actions}
          strategy={data.to_act === 'I' ? cd.strategy : undefined}
        />
      </div>
    </div>
  );
}

function RangePanel({ label, stats, color }: { label: string; stats: RangeStats; color: 'oop' | 'ip' }) {
  const dot = color === 'oop' ? 'bg-oop' : 'bg-ip';
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-3">
      <div className="flex items-baseline justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={`inline-block w-2 h-2 rounded-full ${dot}`} />
          <span className="font-semibold text-sm">{label}</span>
        </div>
        <div className="font-mono text-xl tabular-nums">{pct(stats.range_eq)}</div>
      </div>
      <BucketBar stats={stats} />
      <Histogram bins={stats.hist} />
      <dl className="mt-2 grid grid-cols-5 gap-1 text-[10px]">
        <BucketCell label="nut" v={stats.nut} cls="bg-emerald-700" />
        <BucketCell label="strong" v={stats.strong} cls="bg-emerald-900" />
        <BucketCell label="marg" v={stats.marginal} cls="bg-amber-800" />
        <BucketCell label="weak" v={stats.weak} cls="bg-orange-900" />
        <BucketCell label="air" v={stats.air} cls="bg-neutral-800" />
      </dl>
    </div>
  );
}

function BucketBar({ stats }: { stats: RangeStats }) {
  const segs: Array<[string, number]> = [
    ['bg-emerald-600', stats.nut],
    ['bg-emerald-800', stats.strong],
    ['bg-amber-700',   stats.marginal],
    ['bg-orange-900',  stats.weak],
    ['bg-neutral-700', stats.air],
  ];
  return (
    <div className="flex h-3 w-full overflow-hidden rounded">
      {segs.map(([cls, v], i) => (
        <div key={i} className={cls} style={{ width: `${v * 100}%` }} />
      ))}
    </div>
  );
}

function Histogram({ bins }: { bins: number[] }) {
  const max = Math.max(0.01, ...bins);
  return (
    <div className="mt-2 flex items-end gap-px h-12">
      {bins.map((v, i) => (
        <div
          key={i}
          title={`${(i * 10)}–${(i + 1) * 10}% eq: ${pct(v)}`}
          className="flex-1 bg-neutral-400/70 hover:bg-neutral-200"
          style={{ height: `${(v / max) * 100}%`, minHeight: v > 0 ? '2px' : '0' }}
        />
      ))}
    </div>
  );
}

function BucketCell({ label, v, cls }: { label: string; v: number; cls: string }) {
  return (
    <div className="flex flex-col items-center">
      <div className={`w-full h-1 ${cls} rounded`} />
      <div className="text-neutral-500 mt-0.5">{label}</div>
      <div className="text-neutral-200 tabular-nums">{pct(v, 1)}</div>
    </div>
  );
}

function AdvantageBadges({ record }: { record: NodeRecord }) {
  return (
    <div className="flex gap-3 text-xs">
      <Badge label="range advantage" value={record.range_advantage} />
      <Badge label="nut advantage"   value={record.nut_advantage} />
    </div>
  );
}
function Badge({ label, value }: { label: string; value: string }) {
  const cls =
    value === 'OOP' ? 'text-oop  border-oop/60'  :
    value === 'IP'  ? 'text-ip   border-ip/60'   :
                      'text-neutral-400 border-neutral-700';
  return (
    <span className={`px-2 py-0.5 rounded border ${cls}`}>
      <span className="text-neutral-500 mr-1">{label}:</span>{value}
    </span>
  );
}

function StrategyPanel({
  record, node, onDescend,
}: { record: NodeRecord; node: TrieNode; onDescend: (k: string) => void }) {
  const max = Math.max(0.01, ...record.range_strategy);
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <h3 className="text-sm font-semibold mb-3">
        Strategy <span className="text-neutral-500 font-normal">({record.to_act === 'O' ? 'OOP' : 'IP'} to act)</span>
      </h3>
      <ul className="space-y-2">
        {record.actions.map((a, i) => {
          const freq = record.range_strategy[i] ?? 0;
          const child = node.children.get(a);
          const navigable = !!child;
          return (
            <li key={a}>
              <button
                onClick={() => navigable && onDescend(a)}
                disabled={!navigable}
                className={`w-full text-left rounded border px-3 py-2 transition ${
                  navigable
                    ? 'border-neutral-800 hover:border-neutral-600 bg-neutral-950 cursor-pointer'
                    : 'border-neutral-900 bg-neutral-950 cursor-default opacity-60'
                }`}
                title={navigable ? 'Descend into this action' : 'Terminal (no further records)'}
              >
                <div className="flex items-center justify-between text-sm">
                  <span className="font-mono">{a}</span>
                  <span className="tabular-nums">{pct(freq)}</span>
                </div>
                <div className="mt-1 h-1.5 w-full bg-neutral-800 rounded overflow-hidden">
                  <div
                    className={record.to_act === 'O' ? 'h-full bg-oop' : 'h-full bg-ip'}
                    style={{ width: `${(freq / max) * 100}%` }}
                  />
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ChancePicker({ node, onPick }: { node: TrieNode; onPick: (k: string) => void }) {
  const cards = [...node.children.keys()]
    .filter((k) => k.startsWith('deal_'))
    .map((k) => ({ key: k, card: k.slice('deal_'.length) }));

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <h3 className="text-sm font-semibold mb-3">
        Chance node <span className="text-neutral-500 font-normal">
          ({cards.length} sampled cards — pick one to descend)
        </span>
      </h3>
      <div className="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2">
        {cards.map(({ key, card }) => (
          <button
            key={key}
            onClick={() => onPick(key)}
            className="flex items-center justify-center rounded border border-neutral-800 hover:border-neutral-500 bg-neutral-950 p-2 transition"
            title={key}
          >
            <Card card={card} size="md" />
          </button>
        ))}
      </div>
    </div>
  );
}

function Empty({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-6 text-center text-neutral-500 italic">
      {message}
    </div>
  );
}

function pct(v: number, digits = 1) {
  return `${(v * 100).toFixed(digits)}%`;
}
