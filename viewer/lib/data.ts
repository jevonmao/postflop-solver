import { promises as fs } from 'fs';
import { spawn } from 'child_process';
import readline from 'readline';
import path from 'path';
import type {
  MatchupSummary, NodeRecord, SpotInfo, SpotMeta, Matchup, SpotHeader, NodeCombo,
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

// Matches both the plain dense format (`<stem>.jsonl`) and the zstd-compressed
// combo-v2 format (`<stem>.jsonl.zst`). Group 2 is normalised to a kind tag.
const STEM_RE = /^(\d{4}_[A-Za-z0-9]+)\.(jsonl\.zst|jsonl|meta)$/;

function indexStems(files: string[]): Map<string, { jsonl: boolean; meta: boolean }> {
  const stems = new Map<string, { jsonl: boolean; meta: boolean }>();
  for (const f of files) {
    const m = f.match(STEM_RE);
    if (!m) continue;
    const cur = stems.get(m[1]) ?? { jsonl: false, meta: false };
    if (m[2] === 'jsonl' || m[2] === 'jsonl.zst') cur.jsonl = true;
    if (m[2] === 'meta') cur.meta = true;
    stems.set(m[1], cur);
  }
  return stems;
}

export async function listMatchups(): Promise<MatchupSummary[]> {
  return Promise.all(MATCHUPS.map(async (label) => {
    const dir = path.join(SOLVES_DIR, label);
    const stems = indexStems(await safeReadDir(dir));
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
  const stems = indexStems(await safeReadDir(dir));
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

// ---------------------------------------------------------------------------
// Full-spot parsing + cache
//
// A combo-v2 `.jsonl.zst` spot decompresses to as much as ~800 MB (SRP), almost
// all of it per-combo `combo_data`. We cannot ship the whole tree's combo data
// to the browser, so the page renders aggregate-only records and the client
// fetches one node's combo data at a time via `/api/combo`. To keep node clicks
// instant we decompress + parse a spot once and hold the full result (with
// combo data) in a small server-side LRU.
// ---------------------------------------------------------------------------

interface FullSpot {
  header?: SpotHeader;
  records: NodeRecord[];
  byHistory: Map<string, NodeRecord>;
}

const SPOT_CACHE = new Map<string, FullSpot>();
const SPOT_INFLIGHT = new Map<string, Promise<FullSpot>>();
const SPOT_CACHE_MAX = 2;

function cacheKey(matchup: string, stem: string) { return `${matchup}/${stem}`; }
function histKey(history: string[]) { return history.join(' '); }

function cacheGet(key: string): FullSpot | undefined {
  const v = SPOT_CACHE.get(key);
  if (v) { SPOT_CACHE.delete(key); SPOT_CACHE.set(key, v); } // LRU bump
  return v;
}

function cacheSet(key: string, v: FullSpot) {
  SPOT_CACHE.set(key, v);
  while (SPOT_CACHE.size > SPOT_CACHE_MAX) {
    const oldest = SPOT_CACHE.keys().next().value as string;
    SPOT_CACHE.delete(oldest);
  }
}

// Stream a `.jsonl.zst` (combo-v2) file through the `zstd` CLI line by line.
// The first line is a `{"type":"header",...}` record; the rest are node records.
async function parseZstSpot(filePath: string): Promise<FullSpot> {
  return new Promise((resolve, reject) => {
    const proc = spawn('zstd', ['-dc', filePath], { stdio: ['ignore', 'pipe', 'pipe'] });
    const records: NodeRecord[] = [];
    const byHistory = new Map<string, NodeRecord>();
    let header: SpotHeader | undefined;
    let stderr = '';
    proc.stderr.on('data', (d) => { stderr += d.toString(); });
    proc.on('error', reject);

    const rl = readline.createInterface({ input: proc.stdout });
    rl.on('line', (line) => {
      if (!line) return;
      let obj: any;
      try { obj = JSON.parse(line); } catch { return; }
      if (obj && obj.type === 'header') { header = obj as SpotHeader; return; }
      const rec = obj as NodeRecord;
      records.push(rec);
      byHistory.set(histKey(rec.history ?? []), rec);
    });
    rl.on('close', () => {
      if (proc.exitCode && proc.exitCode !== 0) {
        reject(new Error(`zstd exited ${proc.exitCode}: ${stderr}`));
      } else {
        resolve({ header, records, byHistory });
      }
    });
  });
}

async function parsePlainSpot(filePath: string): Promise<FullSpot> {
  const content = await fs.readFile(filePath, 'utf-8');
  const records: NodeRecord[] = [];
  const byHistory = new Map<string, NodeRecord>();
  for (const line of content.split('\n')) {
    if (!line) continue;
    const obj = JSON.parse(line);
    if (obj && obj.type === 'header') continue;
    const rec = obj as NodeRecord;
    records.push(rec);
    byHistory.set(histKey(rec.history ?? []), rec);
  }
  return { records, byHistory };
}

/** Decompress + parse a spot in full (records keep `combo_data`). Cached. */
async function loadSpotFull(matchup: Matchup, stem: string): Promise<FullSpot> {
  const key = cacheKey(matchup, stem);
  const cached = cacheGet(key);
  if (cached) return cached;
  const inflight = SPOT_INFLIGHT.get(key);
  if (inflight) return inflight;

  const dir = path.join(SOLVES_DIR, matchup);
  const jsonlPath = path.join(dir, `${stem}.jsonl`);
  const zstPath   = path.join(dir, `${stem}.jsonl.zst`);

  const p = (async () => {
    const hasPlain = await fs.access(jsonlPath).then(() => true, () => false);
    const full = hasPlain
      ? await parsePlainSpot(jsonlPath)
      : await parseZstSpot(zstPath);
    cacheSet(key, full);
    return full;
  })();
  SPOT_INFLIGHT.set(key, p);
  try { return await p; }
  finally { SPOT_INFLIGHT.delete(key); }
}

/** Strip `combo_data` — page render ships aggregate records only. */
function stripCombo(r: NodeRecord): NodeRecord {
  if (!r.combo_data) return r;
  const { combo_data, ...rest } = r;
  return rest as NodeRecord;
}

export async function loadSpot(matchup: Matchup, stem: string): Promise<{
  records: NodeRecord[];
  meta?: SpotMeta;
}> {
  const dir = path.join(SOLVES_DIR, matchup);
  const metaTxt = await fs.readFile(path.join(dir, `${stem}.meta`), 'utf-8').catch(() => null);
  const meta = metaTxt ? parseMeta(metaTxt) : undefined;

  const full = await loadSpotFull(matchup, stem);
  return { records: full.records.map(stripCombo), meta };
}

/** Per-combo data for a single decision node, identified by its history path. */
export async function loadNodeCombo(
  matchup: Matchup, stem: string, history: string[],
): Promise<NodeCombo | null> {
  const full = await loadSpotFull(matchup, stem);
  const rec = full.byHistory.get(histKey(history));
  if (!rec || !rec.combo_data || !full.header) return null;
  return {
    combos_oop: full.header.combos_oop,
    combos_ip:  full.header.combos_ip,
    combo_data: rec.combo_data,
    actions:    rec.actions,
    to_act:     rec.to_act,
  };
}
