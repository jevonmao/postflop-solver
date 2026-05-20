//! Tree walker that turns a solved `PostFlopGame` into a stream of records,
//! one per decision node, with range-advantage features alongside the
//! strategy label.
//!
//! Used by both `examples/tree_walker_demo.rs` (interactive inspection) and
//! `examples/dataset_driver.rs` (production dataset generation).

use crate::*;
use std::fmt::Write as _;

/// One decision-node record. Includes both the strategy (label) and the
/// range-advantage features that drive sizing decisions.
#[derive(Clone, Debug)]
pub struct NodeRecord {
    pub matchup:         String,
    pub flop_idx:        u32,
    pub history:         Vec<String>,
    pub board:           Vec<String>,
    pub to_act:          char,    // 'O' or 'I'
    pub pot:             i32,
    pub eff_stack:       i32,
    pub spr:             f32,
    pub actions:         Vec<String>,
    pub oop:             RangeStats,
    pub ip:              RangeStats,
    pub range_advantage: &'static str,
    pub nut_advantage:   &'static str,
    pub range_strategy:  Vec<f32>,
    /// Present when WalkConfig::emit_combo_data is true.
    pub combo_data:      Option<ComboData>,
}

/// Per-combo data for a single decision node, **sparse**: only combos with
/// nonzero reach weight are recorded. Indices reference the file-level header's
/// `combos_oop` / `combos_ip` arrays.
///
/// `strategy` is aligned with the actor's sparse-idx list (the one matching
/// the player-to-act). Each row is either:
///   - an `i32` (>=0): "pure" strategy → action index
///   - a `Vec<f32>`: full distribution over actions (used when no single
///     action gets ≥99.5% of the weight)
#[derive(Clone, Debug)]
pub struct ComboData {
    pub oop: SparsePerCombo,
    pub ip:  SparsePerCombo,
    pub strategy: Vec<StrategyEntry>,
}

#[derive(Clone, Debug, Default)]
pub struct SparsePerCombo {
    pub idx:    Vec<u32>,  // index into header.combos_*
    pub eq:     Vec<f32>,
    pub weight: Vec<f32>,
    pub ev:     Vec<i32>,  // EV in chips, rounded to nearest integer
}

#[derive(Clone, Debug)]
pub enum StrategyEntry {
    Pure(u8),         // action index when one action dominates
    Mixed(Vec<f32>),  // full distribution otherwise
}

#[derive(Default, Clone, Debug)]
pub struct RangeStats {
    pub range_eq:  f32,
    pub nut:       f32,
    pub strong:    f32,
    pub marginal:  f32,
    pub weak:      f32,
    pub air:       f32,
    pub histogram: [f32; 10],
}

#[derive(Clone, Copy)]
pub struct WalkConfig {
    /// Sample N of the 49 possible turn cards per flop subtree.
    /// 0 means "all 49". For dataset production, ~8–12 is a good default.
    pub n_turn_samples: usize,
    /// Sample N of the 48 possible river cards per turn subtree.
    /// 0 means "all 48".
    pub n_river_samples: usize,
    /// Safety cap on total records emitted per walk.
    pub record_limit: usize,
    /// When true, emit per-combo equity, weights, EV, and strategy alongside
    /// the range-aggregate fields. Multiplies record size by ~40–100x.
    pub emit_combo_data: bool,
}

impl Default for WalkConfig {
    fn default() -> Self {
        Self { n_turn_samples: 8, n_river_samples: 6, record_limit: 200_000, emit_combo_data: false }
    }
}

/// Walk a solved game and append records to `out`. The game is mutated during
/// traversal (navigated via `play` + `apply_history`) but is left at the root
/// when the walk completes.
pub fn walk(
    game: &mut PostFlopGame,
    matchup: &str,
    flop_idx: u32,
    cfg: WalkConfig,
    out: &mut Vec<NodeRecord>,
) {
    let mut action_history: Vec<usize> = Vec::new();
    let mut label_history:  Vec<String> = Vec::new();
    walk_rec(game, matchup, flop_idx, cfg, &mut action_history, &mut label_history, out);
    game.back_to_root();
}

fn walk_rec(
    game: &mut PostFlopGame,
    matchup: &str,
    flop_idx: u32,
    cfg: WalkConfig,
    action_history: &mut Vec<usize>,
    label_history: &mut Vec<String>,
    out: &mut Vec<NodeRecord>,
) {
    if out.len() >= cfg.record_limit { return; }
    if game.is_terminal_node() { return; }

    if game.is_chance_node() {
        let mask = game.possible_cards();
        let cards: Vec<u8> = (0u8..52).filter(|c| mask & (1u64 << c) != 0).collect();
        let n_sample = match game.current_board().len() {
            3 => cfg.n_turn_samples,
            4 => cfg.n_river_samples,
            _ => cards.len(),
        };
        for card in sample_uniform(&cards, n_sample) {
            game.play(card as usize);
            action_history.push(card as usize);
            label_history.push(format!(
                "deal_{}",
                card_to_string(card).unwrap_or_else(|_| "??".into())
            ));
            walk_rec(game, matchup, flop_idx, cfg, action_history, label_history, out);
            label_history.pop();
            action_history.pop();
            game.apply_history(action_history);
        }
    } else {
        out.push(build_record(game, matchup, flop_idx, label_history, cfg.emit_combo_data));
        let actions = game.available_actions();
        for (a_idx, action) in actions.iter().enumerate() {
            game.play(a_idx);
            action_history.push(a_idx);
            label_history.push(action_label(action));
            walk_rec(game, matchup, flop_idx, cfg, action_history, label_history, out);
            label_history.pop();
            action_history.pop();
            game.apply_history(action_history);
        }
    }
}

fn sample_uniform(cards: &[u8], n: usize) -> Vec<u8> {
    if n == 0 || n >= cards.len() { return cards.to_vec(); }
    // Deterministic stride sample — reproducible without RNG dep.
    let mut out = Vec::with_capacity(n);
    let stride = cards.len() as f32 / n as f32;
    for i in 0..n {
        let idx = ((i as f32 + 0.5) * stride) as usize;
        out.push(cards[idx.min(cards.len() - 1)]);
    }
    out
}

fn action_label(a: &Action) -> String {
    match a {
        Action::Fold       => "fold".into(),
        Action::Check      => "check".into(),
        Action::Call       => "call".into(),
        Action::Bet(amt)   => format!("bet_{}",   amt),
        Action::Raise(amt) => format!("raise_to_{}", amt),
        Action::AllIn(amt) => format!("allin_{}", amt),
        Action::Chance(c)  => format!("deal_{}",  card_to_string(*c).unwrap_or_else(|_| "??".into())),
        Action::None       => "none".into(),
    }
}

fn build_record(
    game: &mut PostFlopGame,
    matchup: &str,
    flop_idx: u32,
    label_history: &[String],
    emit_combo_data: bool,
) -> NodeRecord {
    game.cache_normalized_weights();

    let board: Vec<String> = game.current_board().into_iter()
        .map(|c| card_to_string(c).unwrap_or_else(|_| "??".into()))
        .collect();

    let cur_player = game.current_player();
    let to_act = if cur_player == 0 { 'O' } else { 'I' };

    let total_bets = game.total_bet_amount();
    let starting_pot = game.tree_config().starting_pot;
    let pot = starting_pot + total_bets[0] + total_bets[1];
    let eff_stack = game.tree_config().effective_stack - total_bets[0].max(total_bets[1]);
    let spr = if pot == 0 { 0.0 } else { eff_stack as f32 / pot as f32 };

    let actions: Vec<String> = game.available_actions().iter().map(action_label).collect();

    let eq_oop = game.equity(0);
    let w_oop  = game.normalized_weights(0).to_vec();
    let eq_ip  = game.equity(1);
    let w_ip   = game.normalized_weights(1).to_vec();
    let oop = compute_range_stats(&eq_oop, &w_oop);
    let ip  = compute_range_stats(&eq_ip,  &w_ip);

    let range_advantage = classify_advantage(oop.range_eq - ip.range_eq, 0.04);
    let nut_advantage   = classify_advantage(oop.nut       - ip.nut,       0.03);

    let strat = game.strategy();
    let n_actor = if cur_player == 0 { w_oop.len() } else { w_ip.len() };
    let mut range_strategy = vec![0.0f32; actions.len()];
    let mut wsum = 0.0f32;
    {
        let cur_w = if cur_player == 0 { &w_oop } else { &w_ip };
        for i in 0..n_actor {
            wsum += cur_w[i];
            for (a, f) in range_strategy.iter_mut().enumerate() {
                *f += cur_w[i] * strat[i + a * n_actor];
            }
        }
    }
    if wsum > 0.0 { for f in range_strategy.iter_mut() { *f /= wsum; } }

    let combo_data = if emit_combo_data {
        let oop_ev = game.expected_values(0);
        let ip_ev  = game.expected_values(1);
        let n_a = actions.len();
        const W_EPS: f32 = 1e-6;

        let oop = sparsify(&eq_oop, &w_oop, &oop_ev, W_EPS);
        let ip  = sparsify(&eq_ip,  &w_ip,  &ip_ev,  W_EPS);

        // Strategy aligned with actor's sparse idx order.
        let actor_w   = if cur_player == 0 { &w_oop }  else { &w_ip  };
        let actor_idx = if cur_player == 0 { &oop.idx } else { &ip.idx };
        let strategy: Vec<StrategyEntry> = actor_idx.iter().map(|&i| {
            let i = i as usize;
            let _ = actor_w[i]; // bounds sanity
            // Read action probs for this combo.
            let probs: Vec<f32> = (0..n_a).map(|a| strat[i + a * n_actor]).collect();
            // Pure if one action ≥ 99.5%.
            let (best_a, best_p) = probs.iter().enumerate()
                .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(i, &p)| (i, p)).unwrap_or((0, 0.0));
            if best_p >= 0.995 {
                StrategyEntry::Pure(best_a as u8)
            } else {
                StrategyEntry::Mixed(probs)
            }
        }).collect();

        Some(ComboData { oop, ip, strategy })
    } else {
        None
    };

    NodeRecord {
        matchup: matchup.to_string(),
        flop_idx,
        history: label_history.to_vec(),
        board,
        to_act, pot, eff_stack, spr,
        actions,
        oop, ip,
        range_advantage, nut_advantage,
        range_strategy,
        combo_data,
    }
}

pub fn compute_range_stats(eq: &[f32], w: &[f32]) -> RangeStats {
    let total_w: f32 = w.iter().sum();
    let mut range_eq = 0.0;
    let (mut nut, mut strong, mut marginal, mut weak, mut air) = (0.0, 0.0, 0.0, 0.0, 0.0);
    let mut histogram = [0.0f32; 10];
    for (&e, &wi) in eq.iter().zip(w.iter()) {
        range_eq += wi * e;
        if      e > 0.85 { nut      += wi; }
        else if e > 0.65 { strong   += wi; }
        else if e > 0.40 { marginal += wi; }
        else if e > 0.20 { weak     += wi; }
        else             { air      += wi; }
        let bin = ((e * 10.0) as usize).min(9);
        histogram[bin] += wi;
    }
    let norm = total_w.max(1e-9);
    RangeStats {
        range_eq:  range_eq / norm,
        nut:       nut      / norm,
        strong:    strong   / norm,
        marginal:  marginal / norm,
        weak:      weak     / norm,
        air:       air      / norm,
        histogram: histogram.map(|h| h / norm),
    }
}

fn classify_advantage(diff: f32, threshold: f32) -> &'static str {
    if diff >  threshold { "OOP" }
    else if diff < -threshold { "IP" }
    else { "EVEN" }
}

fn sparsify(eq: &[f32], w: &[f32], ev: &[f32], w_eps: f32) -> SparsePerCombo {
    let cap = w.iter().filter(|&&x| x > w_eps).count();
    let mut out = SparsePerCombo {
        idx:    Vec::with_capacity(cap),
        eq:     Vec::with_capacity(cap),
        weight: Vec::with_capacity(cap),
        ev:     Vec::with_capacity(cap),
    };
    for (i, &wi) in w.iter().enumerate() {
        if wi > w_eps {
            out.idx.push(i as u32);
            out.eq.push(eq[i]);
            out.weight.push(wi);
            out.ev.push(ev[i].round() as i32);
        }
    }
    out
}

/// Format a `(Card, Card)` pair as a 4-char string like "AcAd".
/// Uses the same single-char rank/suit alphabet as `card_to_string`.
pub fn combo_to_string(c1: u8, c2: u8) -> String {
    let mut s = String::with_capacity(4);
    s.push_str(&card_to_string(c1).unwrap_or_else(|_| "??".into()));
    s.push_str(&card_to_string(c2).unwrap_or_else(|_| "??".into()));
    s
}

/// One file header line emitted before any node records when combo data
/// is enabled. Embeds enough state for a pure-Python decoder to reconstruct
/// the dense per-combo arrays without re-running the Rust solver.
pub fn header_json(
    matchup: &str,
    flop_idx: u32,
    flop: &[u8; 3],
    starting_pot: i32,
    effective_stack: i32,
    combos_oop: &[(u8, u8)],
    combos_ip:  &[(u8, u8)],
) -> String {
    let flop_strs: Vec<String> = flop.iter()
        .map(|c| card_to_string(*c).unwrap_or_else(|_| "??".into())).collect();
    let oop_strs:  Vec<String> = combos_oop.iter().map(|&(a,b)| combo_to_string(a,b)).collect();
    let ip_strs:   Vec<String> = combos_ip.iter().map(|&(a,b)| combo_to_string(a,b)).collect();
    let mut s = String::with_capacity(64 + 5 * (oop_strs.len() + ip_strs.len()));
    s.push('{');
    write!(s, "\"type\":\"header\",").unwrap();
    write!(s, "\"schema\":\"combo-v2\",").unwrap();
    write!(s, "\"matchup\":\"{}\",", matchup).unwrap();
    write!(s, "\"flop_idx\":{},", flop_idx).unwrap();
    write!(s, "\"flop\":{},", json_strs(&flop_strs)).unwrap();
    write!(s, "\"starting_pot\":{},", starting_pot).unwrap();
    write!(s, "\"effective_stack\":{},", effective_stack).unwrap();
    write!(s, "\"combos_oop\":{},", json_strs(&oop_strs)).unwrap();
    write!(s, "\"combos_ip\":{}",   json_strs(&ip_strs)).unwrap();
    s.push('}');
    s
}

// ==================================================================
// JSON serialization (hand-written to avoid serde dep)
// ==================================================================

pub fn record_to_json(r: &NodeRecord) -> String {
    let mut s = String::with_capacity(1024);
    s.push('{');
    write!(s, "\"matchup\":\"{}\",",        r.matchup).unwrap();
    write!(s, "\"flop_idx\":{},",           r.flop_idx).unwrap();
    write!(s, "\"history\":{},",            json_strs(&r.history)).unwrap();
    write!(s, "\"board\":{},",              json_strs(&r.board)).unwrap();
    write!(s, "\"to_act\":\"{}\",",         r.to_act).unwrap();
    write!(s, "\"pot\":{},",                r.pot).unwrap();
    write!(s, "\"eff_stack\":{},",          r.eff_stack).unwrap();
    write!(s, "\"spr\":{:.3},",             r.spr).unwrap();
    write!(s, "\"actions\":{},",            json_strs(&r.actions)).unwrap();
    write!(s, "\"oop\":{},",                range_stats_json(&r.oop)).unwrap();
    write!(s, "\"ip\":{},",                 range_stats_json(&r.ip)).unwrap();
    write!(s, "\"range_advantage\":\"{}\",",r.range_advantage).unwrap();
    write!(s, "\"nut_advantage\":\"{}\",",  r.nut_advantage).unwrap();
    write!(s, "\"range_strategy\":{}",      json_floats(&r.range_strategy)).unwrap();
    if let Some(ref cd) = r.combo_data {
        write!(s, ",\"combo_data\":{}", combo_data_json(cd)).unwrap();
    }
    s.push('}');
    s
}

fn range_stats_json(r: &RangeStats) -> String {
    format!(
        "{{\"range_eq\":{:.4},\"nut\":{:.4},\"strong\":{:.4},\"marginal\":{:.4},\"weak\":{:.4},\"air\":{:.4},\"hist\":{}}}",
        r.range_eq, r.nut, r.strong, r.marginal, r.weak, r.air,
        json_floats(&r.histogram)
    )
}

fn json_strs(v: &[String]) -> String {
    let parts: Vec<String> = v.iter().map(|s| format!("\"{}\"", s.replace('"', "\\\""))).collect();
    format!("[{}]", parts.join(","))
}

fn json_floats(v: &[f32]) -> String {
    let parts: Vec<String> = v.iter().map(|f| format!("{:.4}", f)).collect();
    format!("[{}]", parts.join(","))
}

fn combo_data_json(c: &ComboData) -> String {
    let strat_rows: Vec<String> = c.strategy.iter().map(|e| match e {
        StrategyEntry::Pure(a)   => format!("{}", a),
        StrategyEntry::Mixed(ps) => json_floats3(ps),
    }).collect();
    format!(
        "{{\"oop\":{},\"ip\":{},\"strategy\":[{}]}}",
        sparse_per_combo_json(&c.oop),
        sparse_per_combo_json(&c.ip),
        strat_rows.join(","),
    )
}

fn sparse_per_combo_json(s: &SparsePerCombo) -> String {
    format!(
        "{{\"idx\":{},\"eq\":{},\"w\":{},\"ev\":{}}}",
        json_u32s(&s.idx),
        json_floats3(&s.eq),
        json_floats3(&s.weight),
        json_ints(&s.ev),
    )
}

fn json_floats3(v: &[f32]) -> String {
    let parts: Vec<String> = v.iter().map(|f| format!("{:.3}", f)).collect();
    format!("[{}]", parts.join(","))
}
fn json_u32s(v: &[u32]) -> String {
    let parts: Vec<String> = v.iter().map(|x| x.to_string()).collect();
    format!("[{}]", parts.join(","))
}
fn json_ints(v: &[i32]) -> String {
    let parts: Vec<String> = v.iter().map(|x| x.to_string()).collect();
    format!("[{}]", parts.join(","))
}
