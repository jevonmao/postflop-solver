export const MATCHUPS = ['SRP', '3BP', '4BP'] as const;
export type Matchup = (typeof MATCHUPS)[number];

export interface RangeStats {
  range_eq: number;
  nut: number;
  strong: number;
  marginal: number;
  weak: number;
  air: number;
  hist: number[];
}

export interface ComboData {
  oop_equity: number[];
  oop_weights: number[];
  oop_ev: number[];
  ip_equity: number[];
  ip_weights: number[];
  ip_ev: number[];
  strategy: number[][];
}

export interface NodeRecord {
  matchup: string;
  flop_idx: number;
  history: string[];
  board: string[];
  to_act: 'O' | 'I';
  pot: number;
  eff_stack: number;
  spr: number;
  actions: string[];
  oop: RangeStats;
  ip: RangeStats;
  range_advantage: 'OOP' | 'IP' | 'EVEN';
  nut_advantage:   'OOP' | 'IP' | 'EVEN';
  range_strategy: number[];
  combo_data?: ComboData;
}

export interface SpotMeta {
  matchup: string;
  flop_idx: number;
  memory_gb: number;
  compressed: boolean;
  build_s: number;
  solve_s: number;
  walk_s: number;
  exploitability_pct_pot: number;
  n_records: number;
  max_iter: number;
  target_pct: number;
  turn_samples: number;
  river_samples: number;
  combo_data: boolean;
}

export interface SpotInfo {
  stem: string;       // e.g. "0022_2c2dKc"
  idx: number;        // 22
  flop: string;       // "2c2dKc"
  hasJsonl: boolean;
  hasMeta: boolean;
  meta?: SpotMeta;
}

export interface MatchupSummary {
  label: Matchup;
  count: number;      // number of fully-solved spots
  partial: number;    // number with only one of the two files
}
