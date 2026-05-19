// Benchmark: HU 200BB flop solves for the three real preflop scenarios.
// Goal — measure per-spot solve time and memory under HU-realistic configs
// so we can project dataset-generation throughput.
//
// Ranges are loaded from `data/hu_200bb_ranges.txt` (single source of truth).
// Chip units: 1 chip = 0.01 BB. Stacks = 200 BB = 20000 chips.

use postflop_solver::hu_200bb_ranges::{Action as RangeAction, PreflopRanges};
use postflop_solver::*;
use std::time::Instant;

fn main() {
    println!("=== HU 200BB flop-solve throughput benchmark ===\n");

    let r = PreflopRanges::load_default().expect("load preflop ranges");
    let g = |spot, action| r.get(spot, action)
        .unwrap_or_else(|| panic!("range [{spot}/{action:?}] missing from template"))
        .clone();

    // OOP × IP per matchup, plus the chip-units pot/stack:
    let srp_oop = g("BB_VS_SB_RAISE", RangeAction::Call);
    let srp_ip  = g("SB_FIRST_ACTION", RangeAction::Raise);
    let bp3_oop = g("BB_VS_SB_RAISE", RangeAction::Raise);
    let bp3_ip  = g("SB_VS_BB_3BET",  RangeAction::Call);
    let bp4_oop = g("BB_VS_SB_4BET",  RangeAction::Call);
    let bp4_ip  = g("SB_VS_BB_3BET",  RangeAction::Raise);

    // Bet sizes — production config: rich flop, lean turn/river.
    let flop_sizes  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_sizes  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_sizes = BetSizeOptions::try_from(("75%",     "3x")).unwrap();

    let test_flops = ["Kh7d2c", "Th9d8h"];

    println!("{:<5} {:<8} {:>8} {:>9} {:>14} {:>8} {:>9} {:>10}",
             "spot", "flop", "mem GB", "use cmp?", "build s", "solve s", "iters", "expl%");

    // SRP — pot 500 chips, eff 19,750
    for flop in test_flops {
        run_solve("SRP", flop, &srp_oop, &srp_ip, 500, 19_750,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }
    // 3BP — pot 2000, eff 19,000
    for flop in test_flops {
        run_solve("3BP", flop, &bp3_oop, &bp3_ip, 2_000, 19_000,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }
    // 4BP — pot 5000, eff 17,500
    for flop in test_flops {
        run_solve("4BP", flop, &bp4_oop, &bp4_ip, 5_000, 17_500,
                  &flop_sizes, &turn_sizes, &river_sizes);
    }
}

#[allow(clippy::too_many_arguments)]
fn run_solve(
    label: &str, flop: &str,
    oop_range: &Range, ip_range: &Range,
    pot: i32, eff_stack: i32,
    flop_b: &BetSizeOptions, turn_b: &BetSizeOptions, river_b: &BetSizeOptions,
) {
    let card_config = CardConfig {
        range: [oop_range.clone(), ip_range.clone()],
        flop: flop_from_str(flop).unwrap(),
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: pot,
        effective_stack: eff_stack,
        rake_rate: 0.0, rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b.clone()],
        turn_bet_sizes:  [turn_b.clone(),  turn_b.clone()],
        river_bet_sizes: [river_b.clone(), river_b.clone()],
        turn_donk_sizes: None, river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let t0 = Instant::now();
    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    let mem_gb = game.memory_usage().0 as f64 / (1u64 << 30) as f64;
    let use_compress = mem_gb > 18.0;
    game.allocate_memory(use_compress);
    let build_s = t0.elapsed().as_secs_f64();

    let pot_f = pot as f32;
    let target = pot_f * 0.02; // 2% of pot — production quality
    let max_iter = 200;

    let t1 = Instant::now();
    let exploit = solve(&mut game, max_iter, target, false);
    let solve_s = t1.elapsed().as_secs_f64();

    println!("{:<5} {:<8} {:>8.2} {:>9} {:>14.2} {:>8.2} {:>9} {:>9.3}%",
             label, flop, mem_gb,
             if use_compress { "yes" } else { "no" },
             build_s, solve_s, max_iter,
             100.0 * exploit / pot_f);
}
