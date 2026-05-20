// Persistent live solver server — JSON line protocol on stdin/stdout.
//
// Commands (one JSON object per line on stdin):
//
//   {"cmd":"solve","matchup":"4BP","flop":"2c2d2h"}
//   → {"status":"ok","solve_ms":1450,"exploitability_pct":1.83}
//
//   {"cmd":"query","flop_actions":["check"],"turn":"3h","turn_actions":[],"river":null,"river_actions":[]}
//   → {"status":"ok","to_act":"O","pot":2000,"eff_stack":19000,"spr":9.5,
//      "actions":["check","bet_1500","allin_19000"],"strategy":[0.67,0.22,0.11],
//      "oop":{...},"ip":{...},"range_advantage":"OOP","nut_advantage":"OOP"}
//
//   {"cmd":"quit"}
//
// Usage:
//   cargo build --release --example live_solver
//   ./target/release/examples/live_solver  (then write JSON commands to its stdin)
//
// The Python LiveSolver class in solver_wrapper/live.py wraps this binary.

use postflop_solver::dataset_walker::compute_range_stats;
use postflop_solver::hu_200bb_ranges::{Action as RangeAction, PreflopRanges};
use postflop_solver::*;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::time::Instant;

// ---------------------------------------------------------------------------
// Matchup table — identical to dataset_driver.rs
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct MatchupConfig {
    label:           &'static str,
    oop_spot:        &'static str,
    oop_action:      RangeAction,
    ip_spot:         &'static str,
    ip_action:       RangeAction,
    starting_pot:    i32,
    effective_stack: i32,
}

const MATCHUP_TABLE: &[MatchupConfig] = &[
    MatchupConfig {
        label: "SRP",
        oop_spot: "BB_VS_SB_RAISE", oop_action: RangeAction::Call,
        ip_spot:  "SB_FIRST_ACTION", ip_action:  RangeAction::Raise,
        starting_pot: 500, effective_stack: 19_750,
    },
    MatchupConfig {
        label: "3BP",
        oop_spot: "BB_VS_SB_RAISE", oop_action: RangeAction::Raise,
        ip_spot:  "SB_VS_BB_3BET",  ip_action:  RangeAction::Call,
        starting_pot: 2_000, effective_stack: 19_000,
    },
    MatchupConfig {
        label: "4BP",
        oop_spot: "BB_VS_SB_4BET",  oop_action: RangeAction::Call,
        ip_spot:  "SB_VS_BB_3BET",  ip_action:  RangeAction::Raise,
        starting_pot: 5_000, effective_stack: 17_500,
    },
];

// ---------------------------------------------------------------------------
// Helpers (private in dataset_walker — duplicated here)
// ---------------------------------------------------------------------------

fn action_label(a: &Action) -> String {
    match a {
        Action::Fold       => "fold".into(),
        Action::Check      => "check".into(),
        Action::Call       => "call".into(),
        Action::Bet(amt)   => format!("bet_{amt}"),
        Action::Raise(amt) => format!("raise_to_{amt}"),
        Action::AllIn(amt) => format!("allin_{amt}"),
        Action::Chance(c)  => format!("deal_{}", card_to_string(*c).unwrap_or_else(|_| "??".into())),
        Action::None       => "none".into(),
    }
}

fn classify_advantage(diff: f32, threshold: f32) -> &'static str {
    if diff > threshold { "OOP" }
    else if diff < -threshold { "IP" }
    else { "EVEN" }
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

fn find_action_index(game: &PostFlopGame, label: &str) -> Result<usize, String> {
    let actions = game.available_actions();
    actions.iter()
        .enumerate()
        .find(|(_, a)| action_label(a) == label)
        .map(|(i, _)| i)
        .ok_or_else(|| {
            let available: Vec<_> = actions.iter().map(action_label).collect();
            format!("Action '{label}' not found; available: {available:?}")
        })
}

fn navigate(
    game: &mut PostFlopGame,
    flop_actions: &[String],
    turn: Option<&str>,
    turn_actions: &[String],
    river: Option<&str>,
    river_actions: &[String],
) -> Result<(), String> {
    game.back_to_root();

    for label in flop_actions {
        let idx = find_action_index(game, label)?;
        game.play(idx);
    }

    if let Some(tc) = turn {
        if !game.is_chance_node() {
            return Err(format!(
                "Expected chance node for turn card '{tc}', but got a player/terminal node. \
                 Check that flop actions are correct."
            ));
        }
        let card = card_from_str(tc).map_err(|e| format!("turn card parse error: {e}"))?;
        game.play(card as usize);
        for label in turn_actions {
            let idx = find_action_index(game, label)?;
            game.play(idx);
        }
    }

    if let Some(rc) = river {
        if !game.is_chance_node() {
            return Err(format!(
                "Expected chance node for river card '{rc}', but got a player/terminal node. \
                 Check that turn actions are correct."
            ));
        }
        let card = card_from_str(rc).map_err(|e| format!("river card parse error: {e}"))?;
        game.play(card as usize);
        for label in river_actions {
            let idx = find_action_index(game, label)?;
            game.play(idx);
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Strategy extraction
// ---------------------------------------------------------------------------

fn build_query_response(game: &mut PostFlopGame) -> Value {
    game.cache_normalized_weights();

    let cur_player = game.current_player();
    let to_act = if cur_player == 0 { "O" } else { "I" };

    let total_bets = game.total_bet_amount();
    let starting_pot = game.tree_config().starting_pot;
    let pot = starting_pot + total_bets[0] + total_bets[1];
    let eff_stack = game.tree_config().effective_stack - total_bets[0].max(total_bets[1]);
    let spr = if pot == 0 { 0.0_f32 } else { eff_stack as f32 / pot as f32 };

    let actions: Vec<String> = game.available_actions().iter().map(action_label).collect();

    let eq_oop = game.equity(0);
    let w_oop  = game.normalized_weights(0).to_vec();
    let eq_ip  = game.equity(1);
    let w_ip   = game.normalized_weights(1).to_vec();
    let oop = compute_range_stats(&eq_oop, &w_oop);
    let ip  = compute_range_stats(&eq_ip,  &w_ip);

    let range_advantage = classify_advantage(oop.range_eq - ip.range_eq, 0.04);
    let nut_advantage   = classify_advantage(oop.nut       - ip.nut,       0.03);

    let strat   = game.strategy();
    let n_actor = if cur_player == 0 { w_oop.len() } else { w_ip.len() };
    let cur_w   = if cur_player == 0 { &w_oop } else { &w_ip };
    let mut range_strategy = vec![0.0_f32; actions.len()];
    let mut wsum = 0.0_f32;
    for i in 0..n_actor {
        wsum += cur_w[i];
        for (a, f) in range_strategy.iter_mut().enumerate() {
            *f += cur_w[i] * strat[i + a * n_actor];
        }
    }
    if wsum > 0.0 { for f in range_strategy.iter_mut() { *f /= wsum; } }

    let round2 = |x: f32| (x * 1000.0).round() / 1000.0;

    json!({
        "status": "ok",
        "to_act": to_act,
        "pot": pot,
        "eff_stack": eff_stack,
        "spr": round2(spr),
        "actions": actions,
        "strategy": range_strategy.iter().map(|&x| round2(x)).collect::<Vec<_>>(),
        "oop": {
            "range_eq": round2(oop.range_eq),
            "nut":      round2(oop.nut),
            "strong":   round2(oop.strong),
            "marginal": round2(oop.marginal),
            "weak":     round2(oop.weak),
            "air":      round2(oop.air),
        },
        "ip": {
            "range_eq": round2(ip.range_eq),
            "nut":      round2(ip.nut),
            "strong":   round2(ip.strong),
            "marginal": round2(ip.marginal),
            "weak":     round2(ip.weak),
            "air":      round2(ip.air),
        },
        "range_advantage": range_advantage,
        "nut_advantage":   nut_advantage,
    })
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

enum State {
    Idle,
    Ready(PostFlopGame),
}

fn handle_command(cmd: &Value, state: &mut State, ranges: &PreflopRanges) -> Value {
    match cmd["cmd"].as_str() {
        // ---- solve ----
        Some("solve") => {
            let matchup_label = match cmd["matchup"].as_str() {
                Some(s) => s,
                None => return json!({"status":"error","msg":"missing 'matchup' field"}),
            };
            let flop_str = match cmd["flop"].as_str() {
                Some(s) => s,
                None => return json!({"status":"error","msg":"missing 'flop' field"}),
            };

            let mc = match MATCHUP_TABLE.iter().find(|m| m.label == matchup_label) {
                Some(m) => m,
                None => return json!({"status":"error","msg":format!("unknown matchup '{matchup_label}'; valid: SRP, 3BP, 4BP")}),
            };

            let flop_cards = match flop_from_str(flop_str) {
                Ok(c) => c,
                Err(e) => return json!({"status":"error","msg":format!("flop parse error: {e}")}),
            };

            let oop_range = match ranges.get(mc.oop_spot, mc.oop_action) {
                Some(r) => r.clone(),
                None => return json!({"status":"error","msg":format!("missing OOP range [{}/{}]", mc.oop_spot, mc.oop_action as u8)}),
            };
            let ip_range = match ranges.get(mc.ip_spot, mc.ip_action) {
                Some(r) => r.clone(),
                None => return json!({"status":"error","msg":format!("missing IP range [{}/{}]", mc.ip_spot, mc.ip_action as u8)}),
            };

            let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
            let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
            let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();

            let card_config = CardConfig {
                range: [oop_range, ip_range],
                flop: flop_cards,
                turn: NOT_DEALT,
                river: NOT_DEALT,
            };
            let tree_config = TreeConfig {
                initial_state: BoardState::Flop,
                starting_pot: mc.starting_pot,
                effective_stack: mc.effective_stack,
                rake_rate: 0.0, rake_cap: 0.0,
                flop_bet_sizes:  [flop_b.clone(),  flop_b],
                turn_bet_sizes:  [turn_b.clone(),  turn_b],
                river_bet_sizes: [river_b.clone(), river_b],
                turn_donk_sizes: None, river_donk_sizes: None,
                add_allin_threshold: 1.5,
                force_allin_threshold: 0.15,
                merging_threshold: 0.1,
            };

            let tree = match ActionTree::new(tree_config) {
                Ok(t) => t,
                Err(e) => return json!({"status":"error","msg":format!("tree build error: {e}")}),
            };
            let mut game = match PostFlopGame::with_config(card_config, tree) {
                Ok(g) => g,
                Err(e) => return json!({"status":"error","msg":format!("game init error: {e}")}),
            };

            game.allocate_memory(false);

            let pot = game.tree_config().starting_pot as f32;
            let t0  = Instant::now();
            let expl = solve(&mut game, 200, pot * 0.02, false);
            let solve_ms  = t0.elapsed().as_millis() as u64;
            let expl_pct  = (100.0 * expl / pot * 100.0).round() / 100.0; // 2dp

            *state = State::Ready(game);
            json!({
                "status": "ok",
                "solve_ms": solve_ms,
                "exploitability_pct": expl_pct,
            })
        }

        // ---- query ----
        Some("query") => {
            let game = match state {
                State::Ready(g) => g,
                State::Idle => return json!({"status":"error","msg":"no game solved yet — send a 'solve' command first"}),
            };

            let strs = |v: &Value| -> Vec<String> {
                v.as_array().map(|a| {
                    a.iter().filter_map(|x| x.as_str().map(String::from)).collect()
                }).unwrap_or_default()
            };

            let flop_actions  = strs(&cmd["flop_actions"]);
            let turn          = cmd["turn"].as_str();
            let turn_actions  = strs(&cmd["turn_actions"]);
            let river         = cmd["river"].as_str();
            let river_actions = strs(&cmd["river_actions"]);

            if let Err(e) = navigate(game, &flop_actions, turn, &turn_actions, river, &river_actions) {
                return json!({"status":"error","msg":e});
            }

            build_query_response(game)
        }

        // ---- quit ----
        Some("quit") => {
            std::process::exit(0);
        }

        Some(other) => json!({"status":"error","msg":format!("unknown command '{other}'; valid: solve, query, quit")}),
        None => json!({"status":"error","msg":"missing 'cmd' field"}),
    }
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

fn main() {
    let ranges = match PreflopRanges::load_default() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("live_solver: failed to load preflop ranges: {e}");
            std::process::exit(1);
        }
    };

    let mut state = State::Idle;
    let stdin = io::stdin();

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let line = line.trim();
        if line.is_empty() { continue; }

        let cmd: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                let resp = json!({"status":"error","msg":format!("JSON parse error: {e}")});
                println!("{resp}");
                let _ = io::stdout().flush();
                continue;
            }
        };

        let resp = handle_command(&cmd, &mut state, &ranges);
        println!("{resp}");
        let _ = io::stdout().flush();
    }
}
