// Compare SRP 200BB solve time + memory under three configurations
// against the same flop board, ranges, and stack.
//
// Baseline ("rich"):   33%,75% flop + 75% turn + 75%,a river   (current benchmark)
// Lean:                50%   flop  + 75% turn + 75%   river   (1 size per street)
// Lean + target 2%:    same lean tree, 2% exploitability cap

use postflop_solver::*;
use std::time::Instant;

fn main() {
    let btn_open = "22+,A2s+,K2s+,Q2s+,J4s+,T6s+,96s+,85s+,75s+,64s+,53s+,\
                    A2o+,K5o+,Q8o+,J8o+,T8o+,97o+,87o,76o";
    let bb_call = "JJ-22,AQs-A2s,KJs-K2s,QJs-Q5s,J9s-J6s,T9s-T7s,96s+,85s+,75s+,64s+,\
                   AJo-A2o,KJo-K8o,QJo-Q9o,JTo-J9o,T9o-T8o,98o,87o,76o,65o";

    let rich_flop  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let rich_turn  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let rich_river = BetSizeOptions::try_from(("75%,a",   "3x")).unwrap();

    let lean_flop  = BetSizeOptions::try_from(("50%", "3x")).unwrap();
    let lean_turn  = BetSizeOptions::try_from(("75%", "3x")).unwrap();
    let lean_river = BetSizeOptions::try_from(("75%", "3x")).unwrap();

    // The heaviest single spot from earlier benchmark
    let flop = "Kh7d2c";

    println!("{:<24} {:>10} {:>10} {:>10} {:>14}", "config", "mem (GB)", "solve (s)", "iters", "expl% pot");

    // -- Rich tree, 1% target (current benchmark setting)
    run("rich + target 1%",  flop, bb_call, btn_open, &rich_flop, &rich_turn, &rich_river, 0.01, 300);

    // -- Rich tree, 2% target — loosen exploitability only
    run("rich + target 2%",  flop, bb_call, btn_open, &rich_flop, &rich_turn, &rich_river, 0.02, 300);

    // -- Lean tree, 1% target — shrink bet tree only
    run("lean + target 1%",  flop, bb_call, btn_open, &lean_flop, &lean_turn, &lean_river, 0.01, 300);

    // -- Lean tree, 2% target — combine both levers
    run("lean + target 2%",  flop, bb_call, btn_open, &lean_flop, &lean_turn, &lean_river, 0.02, 300);
}

#[allow(clippy::too_many_arguments)]
fn run(label: &str, flop: &str, oop: &str, ip: &str,
       fb: &BetSizeOptions, tb: &BetSizeOptions, rb: &BetSizeOptions,
       target_pct: f32, max_iter: u32) {
    let card_config = CardConfig {
        range: [oop.parse().unwrap(), ip.parse().unwrap()],
        flop:  flop_from_str(flop).unwrap(),
        turn:  NOT_DEALT,
        river: NOT_DEALT,
    };
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: 500,
        effective_stack: 19_750,
        rake_rate: 0.0,
        rake_cap: 0.0,
        flop_bet_sizes:  [fb.clone(), fb.clone()],
        turn_bet_sizes:  [tb.clone(), tb.clone()],
        river_bet_sizes: [rb.clone(), rb.clone()],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    let mem_gb = game.memory_usage().0 as f64 / (1u64 << 30) as f64;
    let use_compress = mem_gb > 18.0;
    game.allocate_memory(use_compress);

    let pot = game.tree_config().starting_pot as f32;
    let target = pot * target_pct;
    let t0 = Instant::now();

    // Manual loop so we can also report the iteration count actually used.
    let mut iters_used = max_iter;
    for i in 0..max_iter {
        solve_step(&game, i);
        if (i + 1) % 10 == 0 {
            let expl = compute_exploitability(&game);
            if expl <= target { iters_used = i + 1; break; }
        }
    }
    finalize(&mut game);
    let solve_s = t0.elapsed().as_secs_f64();
    let final_expl = compute_exploitability(&game);

    println!("{:<24} {:>10.2} {:>10.2} {:>10} {:>14.3}",
             label, mem_gb, solve_s, iters_used, 100.0 * final_expl / pot);
}
