// 100BB BTN vs BB single-raised pot, flop = Kh7d2c.
//
// Stake unit: 1 chip = 0.01 BB (so 1 BB = 100 chips).
// Preflop assumed: BTN opens 2.5 BB, SB folds, BB calls.
//   Pot going to flop  = 0.5 + 2.5 + 2.5 = 5.5 BB   ->  550 chips
//   Effective stack    = 100 - 2.5      = 97.5 BB   -> 9750 chips
//   SPR on flop        ~ 17.7
//
// OOP = BB (caller), IP = BTN (raiser).

use postflop_solver::*;

fn main() {
    // BB calling range vs 2.5x BTN open (~26% of hands)
    let bb_call_range = "\
        TT-22,\
        AJs-A2s,KJs-K2s,QJs-Q5s,J9s-J7s,T8s-T7s,97s-96s,87s-86s,76s-75s,65s,54s,\
        AJo-A9o,KQo-KTo,QJo-QTo,JTo,T9o";

    // BTN open range (~47% of hands, no 3-bet removal for simplicity)
    let btn_open_range = "\
        22+,\
        A2s+,K2s+,Q4s+,J7s+,T7s+,97s+,86s+,75s+,64s+,54s,\
        A2o+,K9o+,Q9o+,J9o+,T9o,98o";

    let card_config = CardConfig {
        range: [bb_call_range.parse().unwrap(), btn_open_range.parse().unwrap()],
        flop: flop_from_str("Kh7d2c").unwrap(),
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };

    // Single bet size per street to keep the tree small enough to fit in RAM
    // and solve in a reasonable time. All-ins are added by threshold.
    let flop_oop = BetSizeOptions::try_from(("33%", "3x")).unwrap();
    let flop_ip  = BetSizeOptions::try_from(("33%", "3x")).unwrap();
    let turn_oop = BetSizeOptions::try_from(("75%", "3x")).unwrap();
    let turn_ip  = BetSizeOptions::try_from(("75%", "3x")).unwrap();
    let river_oop = BetSizeOptions::try_from(("75%", "3x")).unwrap();
    let river_ip  = BetSizeOptions::try_from(("75%", "3x")).unwrap();

    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: 550,     // 5.5 BB
        effective_stack: 9750, // 97.5 BB
        rake_rate: 0.0,
        rake_cap: 0.0,
        flop_bet_sizes:  [flop_oop,  flop_ip],
        turn_bet_sizes:  [turn_oop,  turn_ip],
        river_bet_sizes: [river_oop, river_ip],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let action_tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, action_tree).unwrap();

    let (mem, mem_c) = game.memory_usage();
    println!("Memory (uncompressed): {:.2} GB", mem   as f64 / (1u64 << 30) as f64);
    println!("Memory (compressed):   {:.2} GB", mem_c as f64 / (1u64 << 30) as f64);

    // ~4.4 GB; well under available RAM. Skip compression for speed.
    game.allocate_memory(false);

    let max_iter: u32 = std::env::var("ITERS").ok().and_then(|s| s.parse().ok()).unwrap_or(200);
    let target = game.tree_config().starting_pot as f32 * 0.005; // 0.5% of pot
    println!("\nSolving up to {} iterations, target exploitability = {:.2} chips ({:.2}% of pot)...",
             max_iter, target, 0.5);

    let t0 = std::time::Instant::now();
    let exploitability = solve(&mut game, max_iter, target, true);
    let secs = t0.elapsed().as_secs_f64();
    println!("\nSolve wall time: {:.2}s  ({:.0} ms / iter)", secs, secs * 1000.0 / max_iter as f64);
    println!("\nFinal exploitability: {:.2} chips ({:.3}% of pot)",
             exploitability,
             100.0 * exploitability / game.tree_config().starting_pot as f32);

    // Root-node summary
    game.cache_normalized_weights();
    let oop_equity = compute_average(&game.equity(0), game.normalized_weights(0));
    let ip_equity  = compute_average(&game.equity(1), game.normalized_weights(1));
    let oop_ev     = compute_average(&game.expected_values(0), game.normalized_weights(0));
    let ip_ev      = compute_average(&game.expected_values(1), game.normalized_weights(1));
    let pot = game.tree_config().starting_pot as f32;
    println!("\n=== Flop Kh7d2c (root) ===");
    println!("  BB  equity {:5.2}%   EV {:6.2} chips ({:5.2} BB, {:5.2}% of pot)",
             100.0 * oop_equity, oop_ev, oop_ev / 100.0, 100.0 * oop_ev / pot);
    println!("  BTN equity {:5.2}%   EV {:6.2} chips ({:5.2} BB, {:5.2}% of pot)",
             100.0 * ip_equity,  ip_ev,  ip_ev  / 100.0, 100.0 * ip_ev  / pot);

    // OOP's strategy at the root: BB action frequencies.
    println!("\n=== BB strategy at root (action frequencies, range-averaged) ===");
    print_strategy(&mut game, /* player = */ 0);

    // BB checks -> BTN's c-bet strategy.
    game.play(0); // BB Check
    println!("\n=== BTN strategy facing BB check ===");
    print_strategy(&mut game, /* player = */ 1);

    // Now show BTN's c-bet sizing on a few specific hands.
    println!("\n--- BTN's strategy with selected hands (Check / Bet33 / AllIn? etc.) ---");
    let actions = game.available_actions();
    println!("  available actions: {:?}", actions);
    let strategy = game.strategy();
    let ip_cards = game.private_cards(1);
    let n = ip_cards.len();
    for hand in ["AhAs", "KsQs", "QcQd", "AdQd", "7s6s", "5h4h"] {
        if let Some(idx) = holes_to_strings(ip_cards).unwrap().iter().position(|s| s == hand) {
            let row: Vec<String> = (0..actions.len())
                .map(|a| format!("{:>6.1}%", 100.0 * strategy[idx + a * n]))
                .collect();
            println!("  {hand}:  {}", row.join(" "));
        }
    }

    game.back_to_root();
}

fn print_strategy(game: &mut PostFlopGame, player: usize) {
    game.cache_normalized_weights();
    let actions = game.available_actions();
    let cards = game.private_cards(player);
    let n = cards.len();
    let strategy = game.strategy();
    let weights = game.normalized_weights(player);
    let mut totals = vec![0.0f32; actions.len()];
    let mut total_w = 0.0f32;
    for i in 0..n {
        let w = weights[i];
        total_w += w;
        for (a, t) in totals.iter_mut().enumerate() {
            *t += w * strategy[i + a * n];
        }
    }
    for (a, t) in totals.iter().enumerate() {
        println!("  {:>16?}: {:>6.2}%", actions[a], 100.0 * t / total_w);
    }
}
