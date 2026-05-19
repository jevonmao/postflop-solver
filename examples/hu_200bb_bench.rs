// Benchmark: HU 200BB flop solves for the three real preflop scenarios.
// Goal — measure per-spot solve time and memory under HU-realistic configs
// so we can project dataset-generation throughput.
//
// Chip units: 1 chip = 0.01 BB. Stacks = 200 BB = 20000 chips.

use postflop_solver::*;
use std::time::Instant;

fn main() {
    println!("=== HU 200BB flop-solve throughput benchmark ===\n");

    // ---- Ranges ----
    // BTN open range (~84% — standard HU GTO opener)
    let btn_open = "22+,A2s+,K2s+,Q2s+,J4s+,T6s+,96s+,85s+,75s+,64s+,53s+,\
                    A2o+,K5o+,Q8o+,J8o+,T8o+,97o+,87o,76o";
    // BB call vs BTN 2.5x open (~55% — wide HU defend)
    let bb_call = "JJ-22,AQs-A2s,KJs-K2s,QJs-Q5s,J9s-J6s,T9s-T7s,96s+,85s+,75s+,64s+,\
                   AJo-A2o,KJo-K8o,QJo-Q9o,JTo-J9o,T9o-T8o,98o,87o,76o,65o";
    // BB 3-bet (~13%) — value + polar bluffs
    let bb_3bet = "TT+,AQs+,AQo+,A5s,A4s,K9s,76s,65s";
    // BTN call vs BB 3-bet (~22%)
    let btn_call_vs_3bet = "99-22,AJs-A2s,KTs-K2s,QTs+,J9s+,T8s+,98s,87s,76s,65s,AJo,KQo";
    // BTN 4-bet (~5%)
    let btn_4bet = "QQ+,AKs,AKo,A5s";
    // BB call vs 4-bet (~3.5%)
    let bb_call_vs_4bet = "QQ-JJ,AKs,AKo";

    // Bet sizes — realistic for 200BB
    //   flop : 33% and 75%   (small c-bet + larger turn-prep)
    //   turn : 75% only      (keep tree manageable; overbet handled by allin threshold)
    //   river: 75% + all-in
    let flop_sizes  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_sizes  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_sizes = BetSizeOptions::try_from(("75%,a",   "3x")).unwrap();

    // Two representative flops: dry rainbow (BTN-favored), wet two-tone (BB-favored)
    let test_flops = ["Kh7d2c", "Th9d8h"];

    // ---- SRP, 200BB ----
    // BTN opens 2.5 BB (250), BB calls. Pot = 500. Eff stack = 19,750.
    let srp_pot: i32 = 500;
    let srp_stack: i32 = 19_750;
    for flop in test_flops {
        run_solve("SRP", flop, bb_call, btn_open,
                  srp_pot, srp_stack,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }

    // ---- 3BP, 200BB ----
    // BTN 2.5x (250), BB 3-bet to 10 BB (1000), BTN calls. Pot = 2000. Eff stack = 19,000.
    let bp3_pot: i32 = 2_000;
    let bp3_stack: i32 = 19_000;
    for flop in test_flops {
        run_solve("3BP", flop, bb_3bet, btn_call_vs_3bet,
                  bp3_pot, bp3_stack,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }

    // ---- 4BP, 200BB ----
    // BTN 2.5x, BB 3-bet to 10 BB, BTN 4-bet to 25 BB (2500), BB calls.
    // Pot = 5000. Eff stack = 17,500. SPR ~3.5 — small tree.
    let bp4_pot: i32 = 5_000;
    let bp4_stack: i32 = 17_500;
    for flop in test_flops {
        run_solve("4BP", flop, bb_call_vs_4bet, btn_4bet,
                  bp4_pot, bp4_stack,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }
}

#[allow(clippy::too_many_arguments)]
fn run_solve(
    label: &str, flop: &str,
    oop_range: &str, ip_range: &str,
    pot: i32, eff_stack: i32,
    flop_b: &BetSizeOptions, turn_b: &BetSizeOptions, river_b: &BetSizeOptions,
) {
    let card_config = CardConfig {
        range: [oop_range.parse().unwrap(), ip_range.parse().unwrap()],
        flop: flop_from_str(flop).unwrap(),
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: pot,
        effective_stack: eff_stack,
        rake_rate: 0.0,
        rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b.clone()],
        turn_bet_sizes:  [turn_b.clone(),  turn_b.clone()],
        river_bet_sizes: [river_b.clone(), river_b.clone()],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let t0 = Instant::now();
    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    let (mem, mem_c) = game.memory_usage();
    let mem_gb = mem as f64 / (1u64 << 30) as f64;
    let mem_c_gb = mem_c as f64 / (1u64 << 30) as f64;

    // Decide compression: if uncompressed >18 GB, use compression
    let use_compress = mem_gb > 18.0;
    let path = if use_compress { "compressed" } else { "uncompressed" };
    game.allocate_memory(use_compress);
    let build_s = t0.elapsed().as_secs_f64();

    let pot_f = pot as f32;
    let target = pot_f * 0.01; // 1% of pot — training-data quality
    let max_iter = 150;

    let t1 = Instant::now();
    let exploit = solve(&mut game, max_iter, target, false);
    let solve_s = t1.elapsed().as_secs_f64();

    println!("{:<5} {:<8} mem {:>6.2}GB ({:>6.2} compressed) [{}]  build {:>5.2}s  solve {:>7.2}s  expl {:>6.3}% of pot",
             label, flop, mem_gb, mem_c_gb, path, build_s, solve_s,
             100.0 * exploit / pot_f);
}
