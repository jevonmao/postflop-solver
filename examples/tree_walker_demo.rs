// Walks a solved PostFlopGame and prints sample records. Uses the shared
// walker in `src/dataset_walker.rs` — this file is now a thin demo.
//
// Run:
//   cargo run --release --example tree_walker_demo
//   SPOT=3bp cargo run --release --example tree_walker_demo
//
// Spots use real ranges from `data/hu_200bb_ranges.txt`.

use postflop_solver::dataset_walker::{record_to_json, walk, NodeRecord, WalkConfig};
use postflop_solver::hu_200bb_ranges::{Action as RangeAction, PreflopRanges};
use postflop_solver::*;
use std::time::Instant;

fn main() {
    let spot = std::env::var("SPOT").unwrap_or_else(|_| "4bp".into());
    println!("--- Test spot: {} ---", spot.to_uppercase());

    let ranges = PreflopRanges::load_default().expect("load preflop ranges");
    let (oop_range, ip_range, pot, eff_stack): (Range, Range, i32, i32) = match spot.as_str() {
        // 4BP: BB calls vs SB 4-bet (OOP), SB 4-bets (IP). Pot ~ 5000 chips, eff ~ 17500.
        "4bp" => (
            ranges.get("BB_VS_SB_4BET", RangeAction::Call).expect("BB_VS_SB_4BET/Call").clone(),
            ranges.get("SB_VS_BB_3BET", RangeAction::Raise).expect("SB_VS_BB_3BET/Raise").clone(),
            5_000, 17_500,
        ),
        // 3BP: BB 3-bets (OOP), SB calls (IP). Pot ~ 2000 chips, eff ~ 19000.
        "3bp" => (
            ranges.get("BB_VS_SB_RAISE", RangeAction::Raise).expect("BB_VS_SB_RAISE/Raise").clone(),
            ranges.get("SB_VS_BB_3BET",  RangeAction::Call ).expect("SB_VS_BB_3BET/Call").clone(),
            2_000, 19_000,
        ),
        // SRP: BB calls (OOP), SB raises (IP). Pot 500, eff 19750.
        "srp" => (
            ranges.get("BB_VS_SB_RAISE", RangeAction::Call ).expect("BB_VS_SB_RAISE/Call").clone(),
            ranges.get("SB_FIRST_ACTION", RangeAction::Raise).expect("SB_FIRST_ACTION/Raise").clone(),
            500, 19_750,
        ),
        other => panic!("unknown SPOT={other}; use srp|3bp|4bp"),
    };

    let card_config = CardConfig {
        range: [oop_range, ip_range],
        flop:  flop_from_str("Kh7d2c").unwrap(),
        turn:  NOT_DEALT,
        river: NOT_DEALT,
    };
    let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: pot,
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
    let mem_gb = game.memory_usage().0 as f64 / (1u64 << 30) as f64;
    game.allocate_memory(mem_gb > 18.0);

    let pot_f = pot as f32;
    let t0 = Instant::now();
    let expl = solve(&mut game, 200, pot_f * 0.02, false);
    println!("Solved in {:.2}s, exploitability {:.3}% of pot",
             t0.elapsed().as_secs_f64(), 100.0 * expl / pot_f);

    // Walk
    let cfg = WalkConfig { n_turn_samples: 4, n_river_samples: 4, ..Default::default() };
    let mut records: Vec<NodeRecord> = Vec::new();
    let walk_t = Instant::now();
    walk(&mut game, &spot.to_uppercase(), 0, cfg, &mut records);
    println!("Walked in {:.2}s → {} decision-node records",
             walk_t.elapsed().as_secs_f64(), records.len());

    // Breakdown by street (board length)
    let (mut f3, mut f4, mut f5) = (0u32, 0u32, 0u32);
    for r in &records {
        match r.board.len() { 3 => f3 += 1, 4 => f4 += 1, 5 => f5 += 1, _ => {} }
    }
    println!("  flop:{f3}  turn:{f4}  river:{f5}");

    println!("\nSample records (first 2):");
    for r in records.iter().take(2) {
        println!("{}", record_to_json(r));
    }
    if let Some(root) = records.first() {
        println!("\nRoot node:");
        println!("  board {:?}  to_act={}  pot={}  spr={:.2}",
                 root.board, root.to_act, root.pot, root.spr);
        println!("  actions {:?}", root.actions);
        println!("  range_strategy {:?}", root.range_strategy.iter()
                 .map(|v| format!("{:.3}", v)).collect::<Vec<_>>());
        println!("  oop eq={:.3} nut={:.3} ; ip eq={:.3} nut={:.3}",
                 root.oop.range_eq, root.oop.nut, root.ip.range_eq, root.ip.nut);
        println!("  range_advantage={} nut_advantage={}",
                 root.range_advantage, root.nut_advantage);
    }
}
