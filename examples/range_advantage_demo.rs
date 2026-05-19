// Demonstrate that range/nut/equity advantage metrics can be extracted at the
// flop root from a solved postflop tree. Run on two contrasting flops to verify
// the numbers match poker intuition.
//
// Expected:
//   Kh7d2c (dry, K-high): BTN range-equity advantage AND nut advantage
//                         (because BTN opens wider and has more Kx and PPs that
//                         BB doesn't 3-bet).
//   Th9d8h (wet, connected): BB nut share should be higher proportionally
//                            (small pairs / suited connectors that connect well).

use postflop_solver::*;

fn main() {
    let btn_open = "22+,A2s+,K2s+,Q2s+,J4s+,T6s+,96s+,85s+,75s+,64s+,53s+,\
                    A2o+,K5o+,Q8o+,J8o+,T8o+,97o+,87o,76o";
    let bb_call = "JJ-22,AQs-A2s,KJs-K2s,QJs-Q5s,J9s-J6s,T9s-T7s,96s+,85s+,75s+,64s+,\
                   AJo-A2o,KJo-K8o,QJo-Q9o,JTo-J9o,T9o-T8o,98o,87o,76o,65o";

    for flop in ["Kh7d2c", "Th9d8h"] {
        println!("\n=== Flop {flop} (SRP, 200 BB) ===");
        let mut game = solve_flop(flop, bb_call, btn_open);
        analyze(&mut game, flop);
    }
}

fn solve_flop(flop: &str, oop: &str, ip: &str) -> PostFlopGame {
    let card_config = CardConfig {
        range: [oop.parse().unwrap(), ip.parse().unwrap()],
        flop: flop_from_str(flop).unwrap(),
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };
    let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: 500,
        effective_stack: 19_750,
        rake_rate: 0.0, rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b],
        turn_bet_sizes:  [turn_b.clone(),  turn_b],
        river_bet_sizes: [river_b.clone(), river_b],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };
    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    game.allocate_memory(false);
    let target = game.tree_config().starting_pot as f32 * 0.02;
    let _ = solve(&mut game, 200, target, false);
    game
}

fn analyze(game: &mut PostFlopGame, flop: &str) {
    game.cache_normalized_weights();

    for (player, name) in [(0, "OOP (BB)"), (1, "IP (BTN)")] {
        let eq = game.equity(player);
        let w  = game.normalized_weights(player);
        let stats = range_stats(&eq, w);

        println!("  {name}:");
        println!("    range equity     : {:>5.2}%", 100.0 * stats.range_eq);
        println!("    nut share  (>85%): {:>5.2}%", 100.0 * stats.nut);
        println!("    strong (65-85%)  : {:>5.2}%", 100.0 * stats.strong);
        println!("    marginal (40-65%): {:>5.2}%", 100.0 * stats.marginal);
        println!("    weak/draw (20-40%): {:>5.2}%", 100.0 * stats.weak);
        println!("    air (<20%)       : {:>5.2}%", 100.0 * stats.air);
        println!("    eq histogram     : {:?}", stats.histogram.map(|h| format!("{:.2}", h)));
    }

    // Derive symbolic advantages
    let oop_stats = range_stats(&game.equity(0), game.normalized_weights(0));
    let ip_stats  = range_stats(&game.equity(1), game.normalized_weights(1));
    let range_adv = classify_advantage(oop_stats.range_eq - ip_stats.range_eq, 0.04);
    let nut_adv   = classify_advantage(oop_stats.nut      - ip_stats.nut,      0.03);
    println!("  --- derived ---");
    println!("    range-equity advantage : {range_adv}");
    println!("    nut advantage          : {nut_adv}");
    println!("    (board: {flop})");

    // Also dump OOP's root strategy so we can see if it tracks
    let actions = game.available_actions();
    let n = game.private_cards(0).len();
    let strat = game.strategy();
    let w0 = game.normalized_weights(0);
    let mut freqs = vec![0.0f32; actions.len()];
    let mut wsum  = 0.0f32;
    for i in 0..n {
        let wi = w0[i];
        wsum += wi;
        for (a, f) in freqs.iter_mut().enumerate() {
            *f += wi * strat[i + a * n];
        }
    }
    println!("    OOP root action freqs  : {}",
             freqs.iter().zip(actions.iter())
                  .map(|(f, a)| format!("{:?}={:.1}%", a, 100.0 * f / wsum))
                  .collect::<Vec<_>>().join("  "));
}

struct RangeStats {
    range_eq: f32,
    nut: f32,
    strong: f32,
    marginal: f32,
    weak: f32,
    air: f32,
    histogram: [f32; 10],
}

fn range_stats(eq: &[f32], w: &[f32]) -> RangeStats {
    let total_w: f32 = w.iter().sum();
    let mut range_eq = 0.0;
    let (mut nut, mut strong, mut marginal, mut weak, mut air) = (0.0, 0.0, 0.0, 0.0, 0.0);
    let mut histogram = [0.0f32; 10];
    for (&e, &wi) in eq.iter().zip(w.iter()) {
        range_eq += wi * e;
        if e > 0.85               { nut       += wi; }
        else if e > 0.65          { strong    += wi; }
        else if e > 0.40          { marginal  += wi; }
        else if e > 0.20          { weak      += wi; }
        else                      { air       += wi; }
        let bin = ((e * 10.0) as usize).min(9);
        histogram[bin] += wi;
    }
    let norm = total_w.max(1e-9);
    RangeStats {
        range_eq: range_eq / norm,
        nut:      nut      / norm,
        strong:   strong   / norm,
        marginal: marginal / norm,
        weak:     weak     / norm,
        air:      air      / norm,
        histogram: histogram.map(|h| h / norm),
    }
}

fn classify_advantage(diff: f32, threshold: f32) -> &'static str {
    if diff >  threshold { "OOP" }
    else if diff < -threshold { "IP" }
    else { "EVEN" }
}
