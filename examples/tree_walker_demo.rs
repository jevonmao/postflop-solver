// Walks a solved PostFlopGame and emits one training record per decision node.
//
// Each record contains:
//   - History (sequence of actions/deals to reach this node)
//   - Board snapshot (flop + optional turn + optional river)
//   - To-act player
//   - Pot, effective stacks remaining, SPR
//   - Available actions
//   - Per-player range stats: range equity, nut/strong/marginal/weak/air buckets,
//     10-bin equity histogram
//   - Derived: range-equity advantage, nut advantage
//   - Range-averaged action frequencies (the supervision label)
//   - Optional sample of per-hand action frequencies
//
// Output format: NDJSON (one JSON object per line). Hand-written serialization
// to avoid adding serde as a dependency to the crate.
//
// Test setup: a 4BP 200BB SRP-style spot on Kh7d2c. SPR ~3.5, solves in seconds,
// fits comfortably in memory. Sufficient to validate walker correctness.

use postflop_solver::*;
use std::fmt::Write as _;
use std::time::Instant;

// --------- walker configuration ---------
const N_TURN_SAMPLES:  usize = 4;   // sample 4 of 49 turns per flop subtree (for the demo)
const N_RIVER_SAMPLES: usize = 4;   // sample 4 of 48 rivers per turn subtree
const _N_HAND_SAMPLES: usize = 0;   // placeholder: per-hand record sampling, not yet implemented
const PRINT_FIRST_N:   usize = 3;   // print this many sample records to stdout
const RECORD_LIMIT:    usize = 50_000; // safety cap

// --------- record structure ---------
struct NodeRecord {
    history:        Vec<String>,
    board:          Vec<String>,    // 3 / 4 / 5 cards
    to_act:         char,           // 'O' or 'I' (Chance nodes don't get records)
    pot:            i32,
    eff_stack:      i32,
    spr:            f32,
    actions:        Vec<String>,
    oop:            RangeStats,
    ip:             RangeStats,
    range_advantage: &'static str,
    nut_advantage:   &'static str,
    range_strategy: Vec<f32>,
}

#[derive(Default, Clone)]
struct RangeStats {
    range_eq:  f32,
    nut:       f32,
    strong:    f32,
    marginal:  f32,
    weak:      f32,
    air:       f32,
    histogram: [f32; 10],
}

fn main() {
    // Pick spot via $SPOT env var ("4bp" default, "3bp" available)
    let spot = std::env::var("SPOT").unwrap_or_else(|_| "4bp".into());
    println!("--- Test spot: {} ---", spot.to_uppercase());

    let (oop_range, ip_range, pot_chips, eff_stack): (&str, &str, i32, i32) = match spot.as_str() {
        "4bp" => (
            "QQ-JJ,AKs,AKo",                                      // BB call vs 4-bet
            "QQ+,AKs,AKo,A5s",                                    // BTN 4-bet
            5_000, 17_500,
        ),
        "3bp" => (
            "TT+,AQs+,AQo+,A5s,A4s,K9s,76s,65s",                  // BB 3-bet
            "99-22,AJs-A2s,KTs-K2s,QTs+,J9s+,T8s+,98s,87s,76s,65s,AJo,KQo", // BTN call vs 3-bet
            2_000, 19_000,
        ),
        other => panic!("Unknown SPOT={other}; use 4bp or 3bp"),
    };
    let card_config = CardConfig {
        range: [oop_range.parse().unwrap(), ip_range.parse().unwrap()],
        flop:  flop_from_str("Kh7d2c").unwrap(),
        turn:  NOT_DEALT,
        river: NOT_DEALT,
    };
    let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: pot_chips,
        effective_stack: eff_stack,
        rake_rate: 0.0, rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b],
        turn_bet_sizes:  [turn_b.clone(),  turn_b],
        river_bet_sizes: [river_b.clone(), river_b],
        turn_donk_sizes: None, river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };
    let action_tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, action_tree).unwrap();
    game.allocate_memory(false);

    let pot = game.tree_config().starting_pot as f32;
    let t0 = Instant::now();
    let expl = solve(&mut game, 200, pot * 0.01, false);
    println!("Solved in {:.2}s, exploitability {:.3}% of pot",
             t0.elapsed().as_secs_f64(), 100.0 * expl / pot);

    // ---------- walk ----------
    let mut records: Vec<NodeRecord> = Vec::new();
    let mut action_history: Vec<usize> = Vec::new();
    let mut label_history: Vec<String> = Vec::new();
    let walk_t0 = Instant::now();
    walk(&mut game, &mut action_history, &mut label_history, &mut records);
    let walk_secs = walk_t0.elapsed().as_secs_f64();

    println!("\nWalked tree in {:.2}s → {} decision-node records emitted", walk_secs, records.len());

    // counts by street (length of board determines street)
    let (mut flop_n, mut turn_n, mut river_n) = (0u32, 0u32, 0u32);
    for r in &records {
        match r.board.len() {
            3 => flop_n  += 1,
            4 => turn_n  += 1,
            5 => river_n += 1,
            _ => {}
        }
    }
    println!("  flop nodes:  {flop_n}\n  turn nodes:  {turn_n}\n  river nodes: {river_n}");

    // ---------- print first N records as NDJSON ----------
    println!("\nSample records (NDJSON, first {} of {}):", PRINT_FIRST_N.min(records.len()), records.len());
    for r in records.iter().take(PRINT_FIRST_N) {
        println!("{}", record_to_json(r));
    }

    // ---------- sanity check: root node features ----------
    if let Some(root) = records.first() {
        println!("\n=== Root node sanity check ===");
        println!("  board: {:?}", root.board);
        println!("  to_act: {}  pot: {}  eff_stack: {}  spr: {:.2}", root.to_act, root.pot, root.eff_stack, root.spr);
        println!("  actions: {:?}", root.actions);
        println!("  range_strategy: {:?}", root.range_strategy.iter().map(|v| format!("{:.3}", v)).collect::<Vec<_>>());
        println!("  oop.range_eq={:.3}  oop.nut={:.3}", root.oop.range_eq, root.oop.nut);
        println!("  ip.range_eq ={:.3}   ip.nut={:.3}", root.ip.range_eq, root.ip.nut);
        println!("  range_advantage={}  nut_advantage={}", root.range_advantage, root.nut_advantage);
    }
}

// =========================================================
// Walker
// =========================================================

fn walk(
    game: &mut PostFlopGame,
    action_history: &mut Vec<usize>,
    label_history: &mut Vec<String>,
    out: &mut Vec<NodeRecord>,
) {
    if out.len() >= RECORD_LIMIT { return; }
    if game.is_terminal_node() { return; }

    if game.is_chance_node() {
        let mask = game.possible_cards();
        let cards: Vec<u8> = (0u8..52).filter(|c| mask & (1u64 << c) != 0).collect();
        let board_len = game.current_board().len();
        let n_sample = match board_len {
            3 => N_TURN_SAMPLES,    // we're about to deal turn
            4 => N_RIVER_SAMPLES,   // we're about to deal river
            _ => cards.len(),
        };
        let sample = sample_uniform(&cards, n_sample);
        for card in sample {
            game.play(card as usize);
            action_history.push(card as usize);
            label_history.push(format!("deal_{}", card_to_string(card).unwrap_or_else(|_| "??".into())));
            walk(game, action_history, label_history, out);
            label_history.pop();
            action_history.pop();
            // Rewind by replaying history from root
            game.apply_history(action_history);
        }
    } else {
        // Decision node — emit a record, then descend through each action
        let record = build_record(game, label_history);
        out.push(record);

        let actions = game.available_actions();
        for (a_idx, action) in actions.iter().enumerate() {
            game.play(a_idx);
            action_history.push(a_idx);
            label_history.push(action_label(action));
            walk(game, action_history, label_history, out);
            label_history.pop();
            action_history.pop();
            game.apply_history(action_history);
        }
    }
}

fn sample_uniform(cards: &[u8], n: usize) -> Vec<u8> {
    if n >= cards.len() { return cards.to_vec(); }
    if n == 0 { return Vec::new(); }
    // Deterministic stride sample — reproducible without RNG dep
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
        Action::Fold        => "fold".into(),
        Action::Check       => "check".into(),
        Action::Call        => "call".into(),
        Action::Bet(amt)    => format!("bet_{}", amt),
        Action::Raise(amt)  => format!("raise_to_{}", amt),
        Action::AllIn(amt)  => format!("allin_{}", amt),
        Action::Chance(c)   => format!("deal_{}", card_to_string(*c).unwrap_or_else(|_| "??".into())),
        Action::None        => "none".into(),
    }
}

// =========================================================
// Record builder — the heart of feature extraction
// =========================================================

fn build_record(game: &mut PostFlopGame, label_history: &Vec<String>) -> NodeRecord {
    game.cache_normalized_weights();

    let board: Vec<String> = game.current_board().into_iter()
        .map(|c| card_to_string(c).unwrap_or_else(|_| "??".into()))
        .collect();

    let cur_player = game.current_player();
    let to_act = if cur_player == 0 { 'O' } else { 'I' };

    // Pot / stacks
    let total_bets = game.total_bet_amount();      // [oop_total, ip_total]
    let starting_pot = game.tree_config().starting_pot;
    let pot = starting_pot + total_bets[0] + total_bets[1];
    let eff_stack_start = game.tree_config().effective_stack;
    let eff_stack_remaining = eff_stack_start - total_bets[0].max(total_bets[1]);
    let spr = if pot == 0 { 0.0 } else { eff_stack_remaining as f32 / pot as f32 };

    let actions: Vec<String> = game.available_actions().iter().map(action_label).collect();

    // Per-player range stats
    let eq_oop = game.equity(0); let w_oop = game.normalized_weights(0).to_vec();
    let eq_ip  = game.equity(1); let w_ip  = game.normalized_weights(1).to_vec();
    let oop = range_stats(&eq_oop, &w_oop);
    let ip  = range_stats(&eq_ip,  &w_ip);

    let range_advantage = classify_adv(oop.range_eq - ip.range_eq, 0.04);
    let nut_advantage   = classify_adv(oop.nut       - ip.nut,       0.03);

    // Range-averaged action frequencies for the to-act player
    let strat = game.strategy();
    let cur_w = if cur_player == 0 { &w_oop } else { &w_ip };
    let n = cur_w.len();
    let mut range_strategy = vec![0.0f32; actions.len()];
    let mut wsum = 0.0f32;
    for i in 0..n {
        let wi = cur_w[i];
        wsum += wi;
        for (a, f) in range_strategy.iter_mut().enumerate() {
            *f += wi * strat[i + a * n];
        }
    }
    if wsum > 0.0 { for f in range_strategy.iter_mut() { *f /= wsum; } }

    NodeRecord {
        history: label_history.clone(),
        board,
        to_act,
        pot,
        eff_stack: eff_stack_remaining,
        spr,
        actions,
        oop, ip,
        range_advantage, nut_advantage,
        range_strategy,
    }
}

fn range_stats(eq: &[f32], w: &[f32]) -> RangeStats {
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

fn classify_adv(diff: f32, threshold: f32) -> &'static str {
    if diff >  threshold { "OOP" }
    else if diff < -threshold { "IP" }
    else { "EVEN" }
}

// =========================================================
// JSON serialization (manual to avoid extra deps)
// =========================================================

fn record_to_json(r: &NodeRecord) -> String {
    let mut s = String::with_capacity(1024);
    s.push('{');
    write!(s, "\"history\":{},",         json_strs(&r.history)).unwrap();
    write!(s, "\"board\":{},",           json_strs(&r.board)).unwrap();
    write!(s, "\"to_act\":\"{}\",",      r.to_act).unwrap();
    write!(s, "\"pot\":{},",             r.pot).unwrap();
    write!(s, "\"eff_stack\":{},",       r.eff_stack).unwrap();
    write!(s, "\"spr\":{:.3},",          r.spr).unwrap();
    write!(s, "\"actions\":{},",         json_strs(&r.actions)).unwrap();
    write!(s, "\"oop\":{},",             range_stats_json(&r.oop)).unwrap();
    write!(s, "\"ip\":{},",              range_stats_json(&r.ip)).unwrap();
    write!(s, "\"range_advantage\":\"{}\",", r.range_advantage).unwrap();
    write!(s, "\"nut_advantage\":\"{}\",",   r.nut_advantage).unwrap();
    write!(s, "\"range_strategy\":{}",   json_floats(&r.range_strategy)).unwrap();
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
