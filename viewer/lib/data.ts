import { promises as fs } from 'fs';
import path from 'path';
import type {
  MatchupSummary, NodeRecord, SpotInfo, SpotMeta, Matchup,
} from './types';
import { MATCHUPS } from './types';

// Resolve the directory that directly contains the matchup folders
// (e.g. <SOLVES_DIR>/4BP/0022_2c2dKc.jsonl).
//
//   1. SOLVES_DIR — explicit path to the matchup parent (preferred).
//   2. DATA_DIR   — legacy; treated as <DATA_DIR>/solves.
//   3. Default    — ../solves_combos relative to the viewer cwd, falling
//                   back to ../data/solves if that doesn't exist.
function resolveSolvesDir(): string {
  if (process.env.SOLVES_DIR) return path.resolve(process.env.SOLVES_DIR);
  if (process.env.DATA_DIR)   return path.join(path.resolve(process.env.DATA_DIR), 'solves');

  const cwd = process.cwd();
  const candidates = [
    path.resolve(cwd, '..', 'solves_combos'),
    path.resolve(cwd, '..', 'data', 'solves'),
  ];
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fsSync = require('fs') as typeof import('fs');
    for (const c of candidates) {
      if (fsSync.existsSync(c)) return c;
    }
  } catch { /* ignore */ }
  return candidates[0];
}

const SOLVES_DIR = resolveSolvesDir();

export function dataPaths() {
  return { SOLVES_DIR };
}

async function safeReadDir(dir: string): Promise<string[]> {
  try { return await fs.readdir(dir); } catch { return []; }
}

export async function listMatchups(): Promise<MatchupSummary[]> {
  return Promise.all(MATCHUPS.map(async (label) => {
    const dir = path.join(SOLVES_DIR, label);
    const files = await safeReadDir(dir);
    const stems = new Map<string, { jsonl: boolean; meta: boolean }>();
    for (const f of files) {
      const m = f.match(/^(\d{4}_[A-Za-z0-9]+)\.(jsonl|meta)$/);
      if (!m) continue;
      const cur = stems.get(m[1]) ?? { jsonl: false, meta: false };
      if (m[2] === 'jsonl') cur.jsonl = true;
      if (m[2] === 'meta')  cur.meta  = true;
      stems.set(m[1], cur);
    }
    let count = 0, partial = 0;
    for (const v of stems.values()) {
      if (v.jsonl && v.meta) count++; else partial++;
    }
    return { label, count, partial };
  }));
}

function parseMeta(s: string): SpotMeta {
  const map: Record<string, string> = {};
  for (const line of s.split('\n')) {
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    map[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  const num = (k: string, d = 0) => Number(map[k] ?? d);
  const bool = (k: string) => map[k] === 'true';
  return {
    matchup: map.matchup ?? '',
    flop_idx: num('flop_idx'),
    memory_gb: num('memory_gb'),
    compressed: bool('compressed'),
    build_s: num('build_s'),
    solve_s: num('solve_s'),
    walk_s: num('walk_s'),
    exploitability_pct_pot: num('exploitability_pct_pot'),
    n_records: num('n_records'),
    max_iter: num('max_iter'),
    target_pct: num('target_pct'),
    turn_samples: num('turn_samples'),
    river_samples: num('river_samples'),
    combo_data: bool('combo_data'),
  };
}

export async function listSpots(matchup: Matchup): Promise<SpotInfo[]> {
  const dir = path.join(SOLVES_DIR, matchup);
  const files = await safeReadDir(dir);
  const stems = new Map<string, { jsonl: boolean; meta: boolean }>();
  for (const f of files) {
    const m = f.match(/^(\d{4}_[A-Za-z0-9]+)\.(jsonl|meta)$/);
    if (!m) continue;
    const cur = stems.get(m[1]) ?? { jsonl: false, meta: false };
    if (m[2] === 'jsonl') cur.jsonl = true;
    if (m[2] === 'meta')  cur.meta  = true;
    stems.set(m[1], cur);
  }
  const infos: SpotInfo[] = [];
  for (const [stem, v] of stems.entries()) {
    const [idxStr, flop] = stem.split('_');
    const info: SpotInfo = {
      stem,
      idx: Number(idxStr),
      flop,
      hasJsonl: v.jsonl,
      hasMeta:  v.meta,
    };
    if (v.meta) {
      try {
        const txt = await fs.readFile(path.join(dir, `${stem}.meta`), 'utf-8');
        info.meta = parseMeta(txt);
      } catch { /* ignore */ }
    }
    infos.push(info);
  }
  infos.sort((a, b) => a.idx - b.idx);
  return infos;
}

export async function loadSpot(matchup: Matchup, stem: string): Promise<{
  records: NodeRecord[];
  meta?: SpotMeta;
}> {
  const dir = path.join(SOLVES_DIR, matchup);
  const jsonlPath = path.join(dir, `${stem}.jsonl`);
  const metaPath  = path.join(dir, `${stem}.meta`);

  const [content, metaTxt] = await Promise.all([
    fs.readFile(jsonlPath, 'utf-8'),
    fs.readFile(metaPath, 'utf-8').catch(() => null),
  ]);

  const records: NodeRecord[] = [];
  for (const line of content.split('\n')) {
    if (!line) continue;
    records.push(JSON.parse(line));
  }
  return { records, meta: metaTxt ? parseMeta(metaTxt) : undefined };
}
