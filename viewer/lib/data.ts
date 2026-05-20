import { promises as fs } from 'fs';
import path from 'path';
import type {
  MatchupSummary, NodeRecord, SpotInfo, SpotMeta, Matchup,
} from './types';
import { MATCHUPS } from './types';

const DATA_DIR = process.env.DATA_DIR
  ?? path.resolve(process.cwd(), '..', 'data');
const SOLVES_DIR = path.join(DATA_DIR, 'solves');

export function dataPaths() {
  return { DATA_DIR, SOLVES_DIR };
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
